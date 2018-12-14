from base import BaseModel
import torch
from model import *
from model.roi_align.roi_align import RoIAlign
from skimage import draw


class PairingGraph(BaseModel):
    def __init__(self, config):
        super(PairingGraph, self).__init__(config)

        checkpoint = torch.load(config['detector_checkpoint'])
        detector_config = config['detector_config'] if 'detector_config' in config else checkpoint['config']['model']
        if 'state_dict' in checkpoint:
            self.detector = eval(checkpoint['config']['arch'])(detector_config)
            self.detector.load_state_dict(checkpoint['state_dict'])
        else:
            self.detector = checkpoint['model']
        self.detector.setForGraphPairing()
        if (config['start_frozen'] if 'start_frozen' in config else False):
            for param in self.detector.parameters(): 
                param.will_use_grad=param.requires_grad 
                param.requires_grad=False 
            self.detector_frozen=True
        else:
            self.detector_frozen=False

        node_channels = config['graph_config']['node_channels']
        edge_channels = config['graph_config']['edge_channels']

        self.roi_align = RoIAlign(self.pool_h,self.pool_w,1.0/self.detector.scale)
        featurizerDescription = config['featurizer'] if 'featurizer' in config else []
        featurizerDescription = [self.detector.last_channels] + featurizerDescription + [edge_channels]
        layers, last_ch = make_layers(featurizerDescription,norm=norm) #we just don't dropout here
        self.edgeFeaturizer = nn.Sequential(layers)

        self.pairer = GraphNet(config['graph_config'])


        self.storedImageName=None

 
    def unfreeze(self): 
        for param in self.detector.parameters(): 
            param.requires_grad=param.will_use_grad 
        self.detector_frozen=False
        

    def forward(self, image, gtBBs=None):
        bbPredictions, offsetPredictions = self.detector(image)
        final_features=self.detector.final_features
        self.detector.final_features=None

        if final_features is None:
            import pdb;pdb.set_trace()

        
        maxConf = bbPredictions[:,:,0].max().item()
        threshConf = max(maxConf*self.confThresh,0.5)
        if self.rotation:
            bbPredictions = non_max_sup_dist(bbPredictions.cpu(),threshConf,2.5)
        else:
            bbPredictions = non_max_sup_iou(bbPredictions.cpu(),threshConf,0.4)
        #I'm assuming batch size of one
        assert(len(bbPredictions)==1)
        bbPredictions=bbPredictions[0]
        #bbPredictions should be switched for GT for training? Then we can easily use BCE loss. 
        #Otherwise we have to to alignment first
        if gtBBs is None:
            useBBs = bbPredictions
        else:
            useBBs = gtBBs
        node_features, adjacencyMatrix, edge_features = self.createGraph(useBBs,final_features)
        nodeOuts, edgeOuts = self.pairer(node_features, adjacencyMatrix, edge_features)

        #adjacencyMatrix = torch.zeros((bbPredictions.size(1),bbPredictions.size(1)))
        #for edge in edgeOuts:
        #    i,j,a=graphToDetectionsMap(

        return bbPredictions, offsetPredictions, edgeOuts #adjacencyMatrix

    def createGraph(self,bbs,features):
        candidates = self.selectCandidateEdges(useBBs)

        #stackedEdgeFeatWindows = torch.FloatTensor((len(candidates),features.size(1)+2,self.edgeWindowSize,self.edgeWindowSize)).to(features.device())
        r = bbs[:,3]
        h = bbs[:,4]
        w = bbs[:,5]
        cos_r = torch.cos(r)
        sin_r = torch.sin(r)
        tlX = -w*cos_r + -h*sin_r
        tlY =  w*sin_r + -h*cos_r
        trX =  w*cos_r + -h*sin_r
        trY = -w*sin_r + -h*cos_r
        brX =  w*cos_r + h*sin_r
        brY = -w*sin_r + h*cos_r
        blX = -w*cos_r + h*sin_r
        blY =  w*sin_r + h*cos_r
        rois = torch.zeros((1,len(candidates),5)) #(batchIndex,x1,y1,x2,y2) as expected by ROI Align
        i=0
        for (index1, index2) in candidates:
            maxX = max(tlX[index1],tlX[index2],trX[index1],trX[index2],blX[index1],blX[index2],brX[index1],brX[index2])
            minX = min(tlX[index1],tlX[index2],trX[index1],trX[index2],blX[index1],blX[index2],brX[index1],brX[index2])
            maxY = max(tlY[index1],tlY[index2],trY[index1],trY[index2],blY[index1],blY[index2],brY[index1],brY[index2])
            minY = min(tlY[index1],tlY[index2],trY[index1],trY[index2],blY[index1],blY[index2],brY[index1],brY[index2])
            rois[i,1]=minX
            rois[i,2]=minY
            rois[i,3]=maxX
            rois[i,4]=maxY
            i+=1
        #crop from feats, ROI pool
        stackedEdgeFeatWindows = self.roi_align(features,rois)

        #create and add masks
        masks = torch.zeros(stackedEdgeFeatWindows.size(0),2,stackedEdgeFeatWindows.size(2),stackedEdgeFeatWindows.size(3))
        for (index1, index2) in candidates:
            #... or make it so index1 is always to top-left one
            if random.random()<0.5:
                temp=index1
                index1=index2
                index2=temp
            
            #warp to roi space
            feature_w = rois[i,3]-rois[i,1] +1
            feature_h = rois[i,4]-rois[i,2] +1
            w_m = self.pool_w/feature_w
            h_m = self.pool_h/feature_h

            tlX1 = math.round((tlX[index1]-rois[i,1]).item()*w_m)
            trX1 = math.round((trX[index1]-rois[i,1]).item()*w_m)
            brX1 = math.round((brX[index1]-rois[i,1]).item()*w_m)
            blX1 = math.round((blX[index1]-rois[i,1]).item()*w_m)
            tlY1 = math.round((tlY[index1]-rois[i,2]).item()*w_h)
            trY1 = math.round((trY[index1]-rois[i,2]).item()*w_h)
            brY1 = math.round((brY[index1]-rois[i,2]).item()*w_h)
            blY1 = math.round((blY[index1]-rois[i,2]).item()*w_h)
            tlX2 = math.round((tlX[index2]-rois[i,1]).item()*w_m)
            trX2 = math.round((trX[index2]-rois[i,1]).item()*w_m)
            brX2 = math.round((brX[index2]-rois[i,1]).item()*w_m)
            blX2 = math.round((blX[index2]-rois[i,1]).item()*w_m)
            tlY2 = math.round((tlY[index2]-rois[i,2]).item()*w_h)
            trY2 = math.round((trY[index2]-rois[i,2]).item()*w_h)
            brY2 = math.round((brY[index2]-rois[i,2]).item()*w_h)
            blY2 = math.round((blY[index2]-rois[i,2]).item()*w_h)

            rr, cc = draw.polygon([tlY1,trY1,brY1,blY1],[tlX1,trX1,brX1,blX1], [self.pool_h,self.pool_w])
            masks[i,0,rr,cc]=1
            rr, cc = draw.polygon([tlY2,trY2,brY2,blY2],[tlX2,trX2,brX2,blX2], [self.pool_h,self.pool_w])
            masks[i,1,rr,cc]=1

        stackedEdgeFeatWindows = torch.cat((stackedEdgeFeatWindows,masks,dim=1)
        edgeFeats = self.edgeFeaturizer(stackedEdgeFeatWindows) #preparing for graph feature size
        #?
        #?crop bbs
        #?run bbs through net
        
        #We're not adding diagonal (self-edges) here!
        #Expecting special handeling during graph conv
        candidateLocs = torch.LongTensor(candidates).t()
        ones = torch.ones(len(candidates))
        adjacencyMatrix = torch.sparse.FloatTensor(candidateLocs,ones,torch.Size([bbs.size(0),bbs.size(0)]))
        edge_features = torch.sparse.FloatTensor(candidateLocs,edgeFeats,torch.Size([bbs.size(0),bbs.size(0),edgeFeats.size(1)]))

        node_features = None
        return node_features, adjacencyMatrix, edge_features



    def selectCandidateEdges(self,bbs):
        #return list of index pairs
        minX = torch.min(bbs[:,0])
        maxX = torch.max(bbs[:,0])
        minY = torch.min(bbs[:,1])
        maxY = torch.max(bbs[:,1])
        maxDim = max( torch.max(bbs[:,3]), torch.max(bbs[:,4]) )

        minX-=maxDim
        minY-=maxDim
        maxX+=maxDim
        minY+=maxDim

        #how to walk?
