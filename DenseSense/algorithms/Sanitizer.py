import DenseSense.algorithms.Algorithm
from DenseSense.algorithms.DensePoseWrapper import DensePoseWrapper
from DenseSense.utils.LMDBHelper import LMDBHelper

import cv2
import matplotlib.pyplot as plt
import numpy as np

import torch
import torch.nn as nn


device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
if torch.cuda.is_available():
    torch.set_default_tensor_type(torch.cuda.FloatTensor)
print("Sanitizer running on: " + str(device))


class Sanitizer(DenseSense.algorithms.Algorithm.Algorithm):
    # UNet, inspired by https://github.com/usuyama/pytorch-unet/
    class MaskGenerator(nn.Module):
        def __init__(self):
            super(Sanitizer.MaskGenerator, self).__init__()

            def double_conv(in_channels, out_channels):
                return nn.Sequential(
                    nn.Conv2d(in_channels, out_channels, 3, padding=1),
                    nn.ReLU(inplace=True),
                    nn.Conv2d(out_channels, out_channels, 3, padding=1),
                    nn.ReLU(inplace=True)
                )

            self.dconv1down = double_conv(1, 8)
            self.dconv2down = double_conv(8, 16)
            self.dconv3down = double_conv(16, 32)

            self.maxpool = nn.MaxPool2d(2)
            self.upsample = nn.Upsample(scale_factor=2, mode='bilinear', align_corners=True)
            self.fc = nn.Linear(2, 14 * 14)
            self.relu = nn.ReLU()

            self.dconvup2 = double_conv(16 + 32, 16)
            self.dconvup1 = double_conv(8 + 16, 8)

            self.conv_last = nn.Conv2d(8, 1, 1)

            self.sigmoid = nn.Sigmoid()

        def forward(self, people):
            if len(people) == 0:
                return np.array([])

            if not isinstance(people, torch.Tensor):
                # Send data to device
                x = torch.Tensor(len(people), 1, 56, 56)
                b = torch.Tensor(len(people), 2)
                for i in range(len(people)):
                    person = people[i]
                    x[i][0] = torch.from_numpy(person.S)
                    bnds = person.bounds
                    area = np.power(np.sqrt((bnds[2] - bnds[0]) * (bnds[3] - bnds[1])), 0.2)
                    if bnds[3] == bnds[1]:
                        aspect = 0
                    else:
                        aspect = (bnds[2] - bnds[0]) / (bnds[3] - bnds[1])
                    b[i] = torch.Tensor([area, aspect])
            else:
                x = people
                b = torch.ones(people.shape[0], 2)

            x = x.to(device)
            b = b.to(device)

            # Normalize input
            x[0 < x] = x[0 < x] / 15.0 * 0.5 + 0.5

            # Run model
            conv1 = self.dconv1down(x)
            x = self.maxpool(conv1)
            conv2 = self.dconv2down(x)
            x = self.maxpool(conv2)

            x = self.dconv3down(x)

            y = self.fc(b)
            y = self.relu(y)
            y = y.view(-1, 1, 14, 14)
            x = x + y

            x = self.upsample(x)
            x = torch.cat([x, conv2], dim=1)

            x = self.dconvup2(x)
            x = self.upsample(x)
            x = torch.cat([x, conv1], dim=1)

            x = self.dconvup1(x)
            x = self.conv_last(x)
            out = self.sigmoid(x)

            return out

    def __init__(self):
        super().__init__()

        # Generate and maybe load mask generator model
        self.maskGenerator = Sanitizer.MaskGenerator()
        self.modelPath = None

        self._training = False
        self._trainingInitiated = False
        self._ROI_masks = torch.Tensor()
        self._ROI_bounds = np.array([])
        self._overlappingROIs = np.array([])
        self._overlappingROIsValues = np.array([])

    def loadModel(self, modelPath):
        self.modelPath = modelPath
        print("Loading Sanitizer MaskGenerator file from: " + self.modelPath)
        self.maskGenerator.load_state_dict(torch.load(self.modelPath, map_location=device))
        self.maskGenerator.to(device)

    def saveModel(self, modelPath):
        if modelPath is None:
            print("Don't know where to save model")
        self.modelPath = modelPath
        print("Saving Sanitizer MaskGenerator model to: "+self.modelPath)
        torch.save(self.maskGenerator.state_dict(), self.modelPath)

    def _initTraining(self, learningRate, dataset, useDatabase):
        # Dataset is COCO
        print("Initiating training of Sanitizer MaskGenerator")
        print("Loading COCO")
        from pycocotools.coco import COCO
        from os import path

        # TODO: support other data sets than Coco
        annFile = './annotations/instances_{}.json'.format(dataset)
        self.cocoPath = './data/{}'.format(dataset)

        self.coco = COCO(annFile)
        self.personCatID = self.coco.getCatIds(catNms=['person'])[0]
        self.cocoImageIds = self.coco.getImgIds(catIds=self.personCatID)

        def isNotCrowd(imgId):
            annIds = self.coco.getAnnIds(imgIds=imgId, catIds=self.personCatID, iscrowd=False)
            annotation = self.coco.loadAnns(annIds)[0]
            return not annotation["iscrowd"]

        self.cocoImageIds = list(filter(isNotCrowd, self.cocoImageIds))
        self.cocoOnDisk = path.exists(self.cocoPath)

        print("Coco dataset size: {}".format(len(self.cocoImageIds)))
        print("Coco images found on disk:", self.cocoOnDisk)

        # Init LMDB_helper
        if useDatabase:
            self.lmdb = LMDBHelper("a")
            self.lmdb.verbose = True

        # Init loss function and optimizer
        self.optimizer = torch.optim.Adam(self.maskGenerator.parameters(), lr=learningRate)

        # Init DensePose extractor
        self.denseposeExtractor = DensePoseWrapper()

    def extract(self, people):
        # Generate masks for all ROIs (people) using neural network model
        self._generateMasks(people)

        # TODO: merge masks and negated masks with segmentation mask from DensePose

        # TODO: find overlapping ROIs and merge the ones where the masks correlate

        # TODO: filter people and update their data

        return people

    def _generateMasks(self, ROIs):
        self._ROI_masks = self.maskGenerator.forward(ROIs)
        self._ROI_bounds = np.zeros((len(ROIs), 4), dtype=np.int32)
        for i in range(len(ROIs)):
            self._ROI_bounds[i] = np.array(ROIs[i].bounds, dtype=np.int32)

    def train(self, epochs=100, learningRate=0.05, dataset="Coco",
              useDatabase=True, printUpdateEvery=40,
              visualize=False, tensorboard=False):

        self._training = True
        if not self._trainingInitiated:
            self._initTraining(learningRate, dataset, useDatabase)

        if tensorboard or type(tensorboard) == str:
            from torch.utils.tensorboard import SummaryWriter
            from PIL import Image

            if type(tensorboard) == str:
                writer = SummaryWriter("./data/tensorboard/"+tensorboard)
            else:
                writer = SummaryWriter("./data/tensorboard/")
            tensorboard = True

            # dummy_input = torch.Tensor(5, 1, 56, 56)
            # writer.add_graph(self.maskGenerator, dummy_input)
            # writer.close()

        Iterations = len(self.cocoImageIds)

        meanPixels = []

        print("Starting training")

        for epoch in range(epochs):
            epochLoss = np.float64(0)
            interestingImage = None
            interestingMeasure = -100000
            for i in range(Iterations):

                # Load instance of COCO dataset
                cocoImage, image = self._getCocoImage(i)
                if image is None: # FIXME
                    print("Image is None??? Skipping.", i)
                    print(cocoImage)
                    continue

                # Get annotation
                annIds = self.coco.getAnnIds(imgIds=cocoImage["id"], catIds=self.personCatID, iscrowd=False)
                annotation = self.coco.loadAnns(annIds)

                # Draw each person in annotation to separate mask
                segs = []
                seg_bounds = []
                for person in annotation:
                    mask = np.zeros(image.shape[0:2], dtype=np.uint8)
                    for s in person["segmentation"]:
                        s = np.reshape(np.array(s, dtype=np.int32), (-2, 2))
                        cv2.fillPoly(mask, [s], 1)
                    segs.append(mask)
                    bbox = person["bbox"]
                    seg_bounds.append(np.array([bbox[0], bbox[1], bbox[0] + bbox[2], bbox[1] + bbox[3]]))

                seg_bounds = np.array(seg_bounds, dtype=np.int32)

                # Get DensePose data from DB or Extractor
                generated = False
                ROIs = None
                if useDatabase:
                    ROIs = self.lmdb.get(DensePoseWrapper, "coco" + str(cocoImage["id"]))
                if ROIs is None:
                    ROIs = self.denseposeExtractor.extract(image)
                    generated = True
                if useDatabase and generated:
                    self.lmdb.save(DensePoseWrapper, "coco" + str(cocoImage["id"]), ROIs)

                # Run prediction
                self._generateMasks(ROIs)

                if len(self._ROI_masks) == 0:
                    continue

                if tensorboard:
                    means = [torch.mean(ROI).detach().cpu().numpy() for ROI in self._ROI_masks]
                    meanPixels.append(sum(means)/len(means))

                # Find overlaps between bboxes of segs and ROIs
                overlaps, overlapLow, overlapHigh = self._overlappingMatrix(
                    seg_bounds.astype(np.int32),
                    self._ROI_bounds.astype(np.int32)
                )

                overlapsInds = np.array(list(zip(*np.where(overlaps))))
                if overlapsInds.shape[0] == 0:
                    continue

                # Get average value where there is overlap between COCO-mask for each person and predictions for
                contentAverage = {}
                for a, b in overlapsInds:  # For every overlap
                    xCoords = np.array([overlapLow[0][a, b], overlapHigh[0][a, b]])
                    yCoords = np.array([overlapLow[1][a, b], overlapHigh[1][a, b]])

                    # ROI transformed overlap area
                    ROI_xCoords = (xCoords - self._ROI_bounds[a][0]) / (self._ROI_bounds[a][2] - self._ROI_bounds[a][0])
                    ROI_xCoords = (ROI_xCoords * 56).astype(np.int32)
                    ROI_xCoords[1] += ROI_xCoords[0] == ROI_xCoords[1]
                    ROI_yCoords = (yCoords - self._ROI_bounds[a][1]) / (self._ROI_bounds[a][3] - self._ROI_bounds[a][1])
                    ROI_yCoords = (ROI_yCoords * 56).astype(np.int32)
                    ROI_yCoords[1] += ROI_yCoords[0] == ROI_yCoords[1]

                    ROI_mask = self._ROI_masks[a, 0][ROI_yCoords[0]:ROI_yCoords[1], ROI_xCoords[0]:ROI_xCoords[1]]

                    # Segmentation overlap area
                    segOverlap = segs[b][yCoords[0]:yCoords[1], xCoords[0]:xCoords[1]]

                    # Transform segmentation
                    segOverlap = cv2.resize(segOverlap, (ROI_mask.shape[1], ROI_mask.shape[0]),
                                            interpolation=cv2.INTER_AREA)

                    # Calculate sum of product of the ROI mask and segment overlap
                    segOverlap = torch.from_numpy(segOverlap).to(device)
                    avgVariable = torch.sum(ROI_mask * segOverlap)
                    avgNegVariable = torch.sum((1 - ROI_mask) * segOverlap)
                    # print("        avgVar", ((ROI_mask * segOverlap).detach().numpy()))
                    # print("        mean", avgVariable.detach().numpy())
                    # print("       -avgVar", (((1 - ROI_mask) * segOverlap).detach().numpy()))
                    # print("       -mean", avgNegVariable.detach().numpy())
                    # print("meanROI", torch.mean(ROI_mask).detach().numpy())

                    # Store this sum
                    if str(a) not in contentAverage:
                        contentAverage[str(a)] = []
                        contentAverage["n"+str(a)] = []

                    contentAverage[str(a)].append(avgVariable)
                    contentAverage["n"+str(a)].append(avgNegVariable)

                # Choose whether to maximize a or -a
                self._overlappingROIs = np.unique(overlapsInds[:, 0])
                self._overlappingROIsValues = np.zeros((self._overlappingROIs.shape[0], 2))

                lossTensor = []
                for j in range(len(self._overlappingROIs)):  # For every ROI with overlap
                    a = self._overlappingROIs[j]

                    A = np.array([float(x.cpu()) for x in contentAverage[str(a)]])
                    AN = np.array([float(x.cpu()) for x in contentAverage["n"+str(a)]])
                    matrix = A[:] + AN[:, None]
                    matrix *= 1 - 10 * np.identity(AN.shape[0])
                    # Choose the two segments which gives the most content
                    aMax = np.unravel_index(matrix.argmax(), matrix.shape)
                    # Add to loss tensor
                    val0 = contentAverage[str(a)][aMax[0]]
                    val1 = contentAverage["n"+str(a)][aMax[1]]

                    if aMax[0] == aMax[1]:
                        s = val0
                        self._overlappingROIsValues[j] = np.array([-val0, -val0])
                    else:
                        s = val0 + val1
                        self._overlappingROIsValues[j] = np.array([val0, val1])

                    lossTensor.append(1.0 / (s + 4))  # TODO: better loss function?

                # Modify weights
                lossSize = torch.stack(lossTensor).mean()
                lossSize.backward()
                self.optimizer.step()
                self.optimizer.zero_grad()
                lossSize = lossSize.cpu().item()

                epochLoss += lossSize/Iterations
                if (i-1) % printUpdateEvery == 0:
                    print("Iteration {} / {}, epoch {} / {}".format(i, Iterations, epoch, epochs))
                    print("Loss size: {}\n".format(lossSize / printUpdateEvery))
                    if tensorboard:
                        absI = i + epoch * Iterations
                        writer.add_scalar("Loss size", lossSize, absI)
                        writer.add_histogram("Mean ROI pixel value", np.array(meanPixels), absI)
                        meanPixels = []

                if tensorboard:
                    interestingness = np.sum(self._overlappingROIsValues)
                    if interestingMeasure < interestingness:
                        interestingImage, shouldUpdate = self.renderDebug(image.copy())
                        interestingMeasure = interestingness

                # Show visualization
                if visualize:
                    image, shouldUpdate = self.renderDebug(image)
                    if shouldUpdate:
                        plt.ion()
                        plt.imshow(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
                        plt.draw()
                        plt.pause(4)

            print("Finished epoch {} / {}. Loss size:".format(epoch, epochs, epochLoss))
            if tensorboard:
                writer.add_scalar("epoch loss size", epochLoss, Iterations*epoch)
                if interestingImage is not None:
                    interestingImage = cv2.cvtColor(interestingImage, cv2.COLOR_BGR2RGB)
                    interestingImage = torch.from_numpy(interestingImage).permute(2, 0, 1)
                    writer.add_image("interesting image", interestingImage, Iterations*epoch)
            self.saveModel(self.modelPath)

        self._training = False

    @staticmethod
    def _overlappingMatrix(a, b):
        xo_high = np.minimum(a[:, 2], b[:, None, 2])
        xo_low = np.maximum(a[:, 0], b[:, None, 0])
        xo = xo_high - xo_low

        yo_high = np.minimum(a[:, 3], b[:, None, 3])
        yo_low = np.maximum(a[:, 1], b[:, None, 1])
        yo = yo_high - yo_low

        overlappingMask = np.logical_and((0 < xo), (0 < yo))
        return overlappingMask, (xo_low, yo_low), (xo_low + xo, yo_low + yo)

    def renderDebug(self, image):
        # Normalize ROIs from (0, 1) to (0, 255)
        ROIsMaskNorm = self._ROI_masks * 255

        # Overlay opacity
        alpha = 0.5

        # Render masks on image
        threshold = 0.05
        shouldUpdate = False
        for i in range(len(self._ROI_masks)):
            if self._training: # FIXME: this should happen for pure inference as well
                # Render only if mask is separating two people
                index = np.where(i == self._overlappingROIs)
                if len(index[0]) != 0:
                    index = index[0][0]
                    if self._overlappingROIsValues[index][0] < threshold or \
                            self._overlappingROIsValues[index][1] < threshold:
                        alpha *= -1.0
                    else:
                        shouldUpdate = True

            mask = ROIsMaskNorm[i, 0].cpu().detach().to(torch.uint8).numpy()
            bnds = self._ROI_bounds[i]

            raw = self._ROI_masks.cpu().detach().numpy()

            # Change colors of mask
            mask = cv2.normalize(mask, None, 0, 255, cv2.NORM_MINMAX) # FIXME: supposed to not be needed
            if 0 < alpha:
                mask = cv2.applyColorMap(mask, cv2.COLORMAP_SUMMER)
            else:
                alpha = -alpha
                mask = cv2.applyColorMap(mask, cv2.COLORMAP_PINK)
            # TODO: render contours instead?
            # Resize mask to bounds
            dims = (bnds[2] - bnds[0], bnds[3] - bnds[1])
            mask = cv2.resize(mask, dims, interpolation=cv2.INTER_AREA)

            # Overlay image
            overlap = image[bnds[1]:bnds[3], bnds[0]:bnds[2]]
            mask = mask * alpha + overlap * (1.0 - alpha)
            image[bnds[1]:bnds[3], bnds[0]:bnds[2]] = mask

        return image, shouldUpdate

    def _getCocoImage(self, index):
        if self.cocoOnDisk:
            # Load image from disk
            cocoImage = self.coco.loadImgs(self.cocoImageIds[index])[0]
            image = cv2.imread(self.cocoPath + "/" + cocoImage["file_name"])
            return cocoImage, image
        else:
            raise FileNotFoundError("COCO image cant be found on disk")
