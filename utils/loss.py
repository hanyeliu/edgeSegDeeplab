import torch
import torch.nn as nn
import logging
import numpy as np
from skimage import io,data,morphology
import cv2
logging.basicConfig(level=logging.WARN)


class SegmentationLosses(object):
    def __init__(self, weight=None, size_average=True, batch_average=True, ignore_index=255, cuda=False,index_list=None,nc=3):
        self.ignore_index = ignore_index
        self.weight = weight
        self.size_average = size_average
        self.batch_average = batch_average
        self.cuda = cuda
        self.index_list = index_list
        self.nc = nc
    def build_loss(self, mode='ce'):
        """Choices: ['ce' or 'focal']"""
        if mode == 'ce':
            return self.CrossEntropyLoss
        elif mode == 'focal':
            return self.FocalLoss
        elif mode =='edgece':
            return self.EdgeCeloss
        else:
            raise NotImplementedError

    def CrossEntropyLoss(self, logit, target):
        n, c, h, w = logit.size()
        criterion = nn.CrossEntropyLoss(weight=self.weight, ignore_index=self.ignore_index,
                                        size_average=self.size_average)
        if self.cuda:
            criterion = criterion.cuda()
        
        loss = criterion(logit[0:self.nc], target.long())

        if self.batch_average:
            loss /= n

        return loss

    def FocalLoss(self, logit, target, gamma=2, alpha=0.5):
        n, c, h, w = logit.size()
        criterion = nn.CrossEntropyLoss(weight=self.weight, ignore_index=self.ignore_index,
                                        size_average=self.size_average)
        if self.cuda:
            criterion = criterion.cuda()

        logpt = -criterion(logit, target.long())
        pt = torch.exp(logpt)
        if alpha is not None:
            logpt *= alpha
        loss = -((1 - pt) ** gamma) * logpt

        if self.batch_average:
            loss /= n

        return loss

    def EdgeCeloss(self, logit, target,alpha=0.2, beta=0.6):
        """
        @ edgeCeloss = edgeLoss+ crossEntropy loss
        """
        n, c, h, w = logit.size()
        # c=3*nc
        # unbalence shape = [n,nc]
        Edge, Weight, unbalence = self.splitMask2Edge(target)
        nc = self.nc
        predSeg = logit[:,0:nc,:,:]
        predEdge = logit[:,nc:,:,:]
        logging.info(f"predEdge size:{predEdge.size()}")
        logging.info(f"predSeg size:{predSeg.size()}")
        lossSeg = self.CrossEntropyLoss(predSeg, target)
        lossEdge = self.edgeLoss(predSeg, predEdge, target, Edge, Weight,unbalence)
        loss = lossSeg + lossEdge
        if self.batch_average:
            loss /= n
        logging.info(f"batch_average :{self.batch_average}")
        return loss
        
    def splitMask2Edge(self, target):
        '''
        @ 将一个mask 按照分类数量分裂成c个mask边界框
        '''
        n,w,h = target.size()
        logging.info(f'n,w,h :{(n,w,h)}')
        logging.info(f'target type:{type(target)}')
        
        if self.index_list==None:
            self.index_list = [i for i in range(self.nc)]
        target_list = torch.chunk(target,n, dim=0)

        logging.info(f'target_list len:{len(target_list)}')
        maskEdgeList  = []
        weightEdgeList = []
        unbalenceList = []
        logging.info(f"target date type:{target.dtype}")
        logging.info(f"index_list:{self.index_list}")
        for target_ in target_list:
            maskList = []
            weightList = []
            unbalenceScalarList = np.zeros([1,self.nc],dtype=np.float)
            
            for index in self.index_list:
                IndexTensor = (target_==index)
                if index==0:
                    IndexTensor = torch.logical_not(IndexTensor)
                IndexTensor = IndexTensor.float()
                edge,weight , unbalenceScalar = self.getEdgeWeight(IndexTensor,blurSize= 7)
                if self.cuda:
                    weight,edge =  weight.cuda(), edge.cuda()
                maskList.append(edge)
                weightList.append(weight)
                unbalenceScalarList[:,index] = unbalenceScalar

            maskEdge = torch.unsqueeze(torch.cat(maskList,axis=0),dim=0)
            weightEdge = torch.unsqueeze(torch.cat(weightList,axis=0),dim=0)
            maskEdgeList.append(maskEdge)
            weightEdgeList.append(weightEdge)
            unbalenceList.append(unbalenceScalarList)

        Edge = torch.cat(maskEdgeList,axis = 0)
        Weight = torch.cat(weightEdgeList,axis = 0)
        unbalence = np.concatenate(unbalenceList,axis = 0)
        logging.info(f"Weight size:{Weight.size()}")
        logging.info(f"Edge size:{Edge.size()}")

        return Edge, Weight,unbalence

    def getEdgeWeight(self,IndexTensor,blurSize = 7):
        
        size = IndexTensor.size()
        logging.info(f"size of indexTensor:{size}")
        
        if len(size)==2:
            if self.cuda:
                imageMask = np.array(IndexTensor.cpu())
            else :
                imageMask = np.array(IndexTensor)
        else:
            if self.cuda:
                imageMask = np.squeeze(np.array(IndexTensor.cpu()),axis=0)
            else:
                imageMask =  np.squeeze(np.array(IndexTensor,axis=0))

        if np.all(imageMask==0):
            weight = torch.zeros(size)
            edge = torch.zeros(size)
            unbalenceScalar = torch.zeros(1)[0]
            return edge, weight,unbalenceScalar

        k = morphology.square(width = 3)      #正方形
        imageOut = morphology.erosion(imageMask, k)
        outlier = imageMask - imageOut
        blur = cv2.GaussianBlur(outlier*255,(blurSize,blurSize),0)/255.
        outlier = np.asarray(outlier!=0,np.float)
        logging.info(f"outlier:{outlier.shape}")
        unbalenceScalar = np.sum(np.asarray(outlier == 0,np.int))/(outlier.shape[0]*outlier.shape[1])
        logging.info(f"观测unbalenceScalar:{unbalenceScalar}")
        unbalenceScalar = torch.from_numpy(unbalenceScalar[None])[0]
        edge = torch.from_numpy(outlier[None,:,:]).float()

        weight = torch.from_numpy(blur[None,:,:]).float()

        return edge, weight, unbalenceScalar

    def edgeLoss2(self,predSeg, predEdge,target, Edge, Weight,unbalence):
        """
        predEdge: [n, nc, h, w]
        edge: [n,nc,h,w]
        wight :[n,nc,h,w]
        math Function:
        """
        softmax = nn.Softmax(dim=1)
        softOut = softmax(predSeg)
        maxIndex = torch.argmax(softOut,dim=1)
        nc = self.nc
        lossEdge = 0
        logging.info(f"unbalence:{unbalence}")
        for n in range(nc):
            # 分割的mask 
            pscMask = maxIndex == n
            gscMask = target==n 
            # 正确分类mask
            segCMaskIndex = pscMask & pscMask
            # 边界分类mask 
            PEdgeN = predEdge[:,n,:,:][segCMaskIndex]
            GEdgeN = Edge[:,n,:,:][segCMaskIndex]
            W = 1 - Weight[:,n,:,:][segCMaskIndex]
            unb = unbalence[:,n].mean()
            ce = torch.nn.functional.binary_cross_entropy_with_logits(W*GEdgeN,PEdgeN )
            lossn = unb*ce
            lossEdge+=lossn
        return lossEdge
    def edgeLoss(self,predSeg, predEdge,target, Edge, Weight,unbalence):
        lossEdge = 0
        for n in range(self.nc):

            PEdgeN = predEdge[:,n,:,:]
            GEdgeN = Edge[:,n,:,:]
            W = 1 - Weight[:,n,:,:]
            unb = unbalence[:,n].mean()
            ce = torch.nn.functional.binary_cross_entropy_with_logits(W*GEdgeN,PEdgeN )
            lossn = unb*ce
            lossEdge+=lossn
        return lossEdge
        
        
if __name__ == "__main__":
    loss = SegmentationLosses(cuda=True)
    a = torch.rand(1, 6, 7, 7).cuda()
    b = torch.rand(1, 7, 7).cuda()
    # print(loss.CrossEntropyLoss(a, b).item())
    logit = a

    target = b
    print(loss.EdgeCeloss(logit, target,alpha=0.2, beta=0.6).item())



