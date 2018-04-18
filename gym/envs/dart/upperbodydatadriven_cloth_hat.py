# This environment is created by Alexander Clegg (alexanderwclegg@gmail.com)

import numpy as np
from gym import utils
from gym.envs.dart.dart_cloth_env import *
from gym.envs.dart.upperbodydatadriven_cloth_base import *
import random
import time
import math

from pyPhysX.colors import *
import pyPhysX.pyutils as pyutils
from pyPhysX.pyutils import LERP
import pyPhysX.renderUtils
import pyPhysX.meshgraph as meshgraph
from pyPhysX.clothfeature import *

import OpenGL.GL as GL
import OpenGL.GLU as GLU
import OpenGL.GLUT as GLUT

class DartClothUpperBodyDataDrivenClothHatEnv(DartClothUpperBodyDataDrivenClothBaseEnv, utils.EzPickle):
    def __init__(self):
        #feature flags
        rendering = True
        clothSimulation = True
        renderCloth = True

        #observation terms
        self.featureInObs   = True  # if true, feature centroid location and displacement from ef are observed
        self.oracleInObs    = True  # if true, oracle vector is in obs
        self.contactIDInObs = True  # if true, contact ids are in obs
        self.hapticsInObs   = True  # if true, haptics are in observation
        self.prevTauObs     = False  # if true, previous action in observation

        #reward flags
        self.uprightReward              = True  #if true, rewarded for 0 torso angle from vertical
        self.limbProgressReward         = True  # if true, the (-inf, 1] plimb progress metric is included in reward
        self.oracleDisplacementReward   = True  # if true, reward ef displacement in the oracle vector direction
        self.contactGeoReward           = True  # if true, [0,1] reward for ef contact geo (0 if no contact, 1 if limbProgress > 0).
        self.deformationPenalty         = True
        self.restPoseReward             = False

        #other flags
        self.hapticsAware       = True  # if false, 0's for haptic input

        #other variables
        self.prevTau = None
        self.maxDeformation = 30.0
        self.restPose = None
        self.prevOracle = np.zeros(3)
        self.prevAvgGeodesic = None
        self.localRightEfShoulder1 = None
        self.limbProgress = 0
        self.previousDeformationReward = 0
        self.state_save_directory = "saved_control_states/"
        self.fingertip = np.array([0.0, -0.095, 0.0])

        self.handleNodeL = None
        self.updateHandleNodeLFrom = 12  # right fingers
        self.handleNodeR = None
        self.updateHandleNodeRFrom = 7  # left fingers

        self.actuatedDofs = np.arange(22)
        observation_size = len(self.actuatedDofs)*3 #q(sin,cos), dq
        if self.prevTauObs:
            observation_size += len(self.actuatedDofs)
        if self.hapticsInObs:
            observation_size += 66
        if self.featureInObs:
            observation_size += 6
        if self.oracleInObs:
            observation_size += 3
        if self.contactIDInObs:
            observation_size += 22

        DartClothUpperBodyDataDrivenClothBaseEnv.__init__(self,
                                                          rendering=rendering,
                                                          screensize=(1080,720),
                                                          clothMeshFile="jacketmedium.obj",#TODO: new mesh
                                                          #clothMeshStateFile = "tshirt_regrip5.obj",
                                                          #clothMeshStateFile = "objFile_1starmin.obj",
                                                          clothScale=np.array([0.7,0.7,0.5]),
                                                          obs_size=observation_size,
                                                          simulateCloth=clothSimulation)

        #clothing features
        self.hatMidVerts = [] #TODO
        self.hatEndVerts = [] #TODO
        self.CP0Feature = ClothFeature(verts=self.hatMidVerts, clothScene=self.clothScene)
        self.CP1Feature = ClothFeature(verts=self.hatEndVerts, clothScene=self.clothScene)

        self.simulateCloth = clothSimulation
        if self.simulateCloth:
            self.handleNodeL = HandleNode(self.clothScene, org=np.array([0.05, 0.034, -0.975]))
            self.handleNodeR = HandleNode(self.clothScene, org=np.array([0.05, 0.034, -0.975]))

        if not renderCloth:
            self.clothScene.renderClothFill = False
            self.clothScene.renderClothBoundary = False
            self.clothScene.renderClothWires = False

        #self.loadCharacterState(filename="characterState_1starmin")

    def _getFile(self):
        return __file__

    def updateBeforeSimulation(self):
        #any pre-sim updates should happen here
        #update features
        if self.CP0Feature is not None:
            self.CP0Feature.fitPlane()
        if self.CP1Feature is not None:
            self.CP1Feature.fitPlane()

        #update handle nodes
        if self.handleNodeL is not None:
            if self.updateHandleNodeLFrom >= 0:
                self.handleNodeL.setTransform(self.robot_skeleton.bodynodes[self.updateHandleNodeLFrom].T)
            self.handleNodeL.step()
        if self.handleNodeR is not None:
            if self.updateHandleNodeRFrom >= 0:
                self.handleNodeR.setTransform(self.robot_skeleton.bodynodes[self.updateHandleNodeRFrom].T)
            self.handleNodeR.step()

        wRFingertip1 = self.robot_skeleton.bodynodes[7].to_world(self.fingertip)
        self.localRightEfShoulder1 = self.robot_skeleton.bodynodes[3].to_local(wRFingertip1)  # right fingertip in right shoulder local frame
        a=0

    def checkTermination(self, tau, s, obs):
        #check the termination conditions and return: done,reward
        topHead = self.robot_skeleton.bodynodes[14].to_world(np.array([0, 0.25, 0]))
        bottomHead = self.robot_skeleton.bodynodes[14].to_world(np.zeros(3))
        bottomNeck = self.robot_skeleton.bodynodes[13].to_world(np.zeros(3))
        if np.amax(np.absolute(s[:len(self.robot_skeleton.q)])) > 10:
            print("Detecting potential instability")
            print(s)
            return True, -500
        elif not np.isfinite(s).all():
            print("Infinite value detected..." + str(s))
            return True, -500
        return False, 0

    def computeReward(self, tau):
        #compute and return reward at the current state
        wRFingertip2 = self.robot_skeleton.bodynodes[7].to_world(self.fingertip)
        wLFingertip2 = self.robot_skeleton.bodynodes[12].to_world(self.fingertip)
        localRightEfShoulder2 = self.robot_skeleton.bodynodes[3].to_local(wRFingertip2)  # right fingertip in right shoulder local frame

        self.prevTau = tau

        reward_limbprogress = 0
        if self.limbProgressReward and self.simulateCloth:
            self.limbProgress = pyutils.limbFeatureProgress(
                limb=pyutils.limbFromNodeSequence(self.robot_skeleton, nodes=self.limbNodesR,
                                                  offset=self.fingertip), feature=self.CP0Feature)
            reward_limbprogress = self.limbProgress
            if reward_limbprogress < 0:  # remove euclidean distance penalty before containment
                reward_limbprogress = 0

        avgContactGeodesic = None
        if self.numSteps > 0 and self.simulateCloth:
            contactInfo = pyutils.getContactIXGeoSide(sensorix=12, clothscene=self.clothScene,
                                                      meshgraph=self.separatedMesh)
            if len(contactInfo) > 0:
                avgContactGeodesic = 0
                for c in contactInfo:
                    avgContactGeodesic += c[1]
                avgContactGeodesic /= len(contactInfo)

        self.prevAvgGeodesic = avgContactGeodesic

        reward_contactGeo = 0
        if self.contactGeoReward and self.simulateCloth:
            if self.limbProgress > 0:
                reward_contactGeo = 1.0
            elif avgContactGeodesic is not None:
                reward_contactGeo = 1.0 - (avgContactGeodesic / self.separatedMesh.maxGeo)
                # reward_contactGeo = 1.0 - minContactGeodesic / self.separatedMesh.maxGeo

        clothDeformation = 0
        if self.simulateCloth:
            clothDeformation = self.clothScene.getMaxDeformationRatio(0)
            self.deformation = clothDeformation

        reward_clothdeformation = 0
        if self.deformationPenalty is True:
            #reward_clothdeformation = (math.tanh(9.24 - 0.5 * clothDeformation) - 1) / 2.0  # near 0 at 15, ramps up to -1.0 at ~22 and remains constant
            reward_clothdeformation = -(math.tanh(0.14*(clothDeformation-25)) + 1)/2.0 # near 0 at 15, ramps up to -1.0 at ~22 and remains constant
        self.previousDeformationReward = reward_clothdeformation
        # force magnitude penalty
        reward_ctrl = -np.square(tau).sum()

        # reward for maintaining posture
        reward_upright = 0
        if self.uprightReward:
            reward_upright = max(-2.5, -abs(self.robot_skeleton.q[0]) - abs(self.robot_skeleton.q[1]))
        reward_oracleDisplacement = 0
        if self.oracleDisplacementReward and np.linalg.norm(self.prevOracle) > 0 and self.localRightEfShoulder1 is not None:
            # world_ef_displacement = wRFingertip2 - wRFingertip1
            relative_displacement = localRightEfShoulder2 - self.localRightEfShoulder1
            oracle0 = self.robot_skeleton.bodynodes[3].to_local(wRFingertip2 + self.prevOracle) - localRightEfShoulder2
            # oracle0 = oracle0/np.linalg.norm(oracle0)
            reward_oracleDisplacement += relative_displacement.dot(oracle0)
        reward_restPose = 0
        if self.restPoseReward and self.restPose is not None:
            z = 0.5  # half the max magnitude (e.g. 0.5 -> [0,1])
            s = 1.0  # steepness (higher is steeper)
            l = 4.2  # translation
            dist = np.linalg.norm(self.robot_skeleton.q - self.restPose)
            #reward_restPose = -(z * math.tanh(s * (dist - l)) + z)
            reward_restPose = max(-51, -dist)
            # print("distance: " + str(dist) + " -> " + str(reward_restPose))

        self.reward = reward_ctrl * 0 \
                      + reward_upright \
                      + reward_limbprogress * 10 \
                      + reward_contactGeo * 2 \
                      + reward_clothdeformation * 5 \
                      + reward_oracleDisplacement * 50 \
                      + reward_restPose
        return self.reward

    def _get_obs(self):
        f_size = 66
        '22x3 dofs, 22x3 sensors, 7x2 targets(toggle bit, cartesian, relative)'
        theta = np.zeros(len(self.actuatedDofs))
        dtheta = np.zeros(len(self.actuatedDofs))
        for ix, dof in enumerate(self.actuatedDofs):
            theta[ix] = self.robot_skeleton.q[dof]
            dtheta[ix] = self.robot_skeleton.dq[dof]

        obs = np.concatenate([np.cos(theta), np.sin(theta), dtheta]).ravel()

        if self.prevTauObs:
            obs = np.concatenate([obs, self.prevTau])

        if self.hapticsInObs:
            f = None
            if self.simulateCloth and self.hapticsAware:
                f = self.clothScene.getHapticSensorObs()#get force from simulation
            else:
                f = np.zeros(f_size)
            obs = np.concatenate([obs, f]).ravel()

        if self.featureInObs and self.simulateCloth:
            centroid = self.CP0Feature.plane.org
            efR = self.robot_skeleton.bodynodes[7].to_world(self.fingertip)
            disp = centroid-efR
            obs = np.concatenate([obs, centroid, disp]).ravel()

        if self.oracleInObs and self.simulateCloth:
            oracle = np.zeros(3)
            if self.reset_number == 0:
                a=0 #nothing
            elif self.limbProgress > 0:
                oracle = self.CP0Feature.plane.normal
            else:
                minContactGeodesic, minGeoVix, _side = pyutils.getMinContactGeodesic(sensorix=12,
                                                                                     clothscene=self.clothScene,
                                                                                     meshgraph=self.separatedMesh,
                                                                                     returnOnlyGeo=False)
                if minGeoVix is None:
                    #oracle points to the garment when ef not in contact
                    efR = self.robot_skeleton.bodynodes[7].to_world(self.fingertip)
                    #closeVert = self.clothScene.getCloseVertex(p=efR)
                    #target = self.clothScene.getVertexPos(vid=closeVert)

                    centroid = self.CP0Feature.plane.org

                    target = centroid
                    vec = target - efR
                    oracle = vec/np.linalg.norm(vec)
                else:
                    vixSide = 0
                    if _side:
                        vixSide = 1
                    if minGeoVix >= 0:
                        oracle = self.separatedMesh.geoVectorAt(minGeoVix, side=vixSide)
            self.prevOracle = oracle
            obs = np.concatenate([obs, oracle]).ravel()

        if self.contactIDInObs:
            HSIDs = self.clothScene.getHapticSensorContactIDs()
            obs = np.concatenate([obs, HSIDs]).ravel()

        return obs

    def additionalResets(self):
        #do any additional resetting here
        self.handFirst = False
        if self.simulateCloth:
            self.clothScene.translateCloth(0, np.array([0.125, -0.27, -0.6]))
        qvel = self.robot_skeleton.dq + self.np_random.uniform(low=-0.01, high=0.01, size=self.robot_skeleton.ndofs)
        qpos = self.robot_skeleton.q + self.np_random.uniform(low=-.01, high=.01, size=self.robot_skeleton.ndofs)
        qpos[16] = 1.9
        '''qpos = np.array(
            [-0.0483053659505, 0.0321213273351, 0.0173036909392, 0.00486290205677, -0.00284350018845, -0.634602301004,
             -0.359172622713, 0.0792754054027, 2.66867203095, 0.00489456931428, 0.000476966442889, 0.0234663491334,
             -0.0254520098678, 0.172782859361, -1.31351102137, 0.702315566312, 1.73993331669, -0.0422811572637,
             0.586669332152, -0.0122329947565, 0.00179736869435, -8.0625896949e-05])
        '''
        self.set_state(qpos, qvel)
        #self.loadCharacterState(filename="characterState_1starmin")
        self.restPose = qpos

        if self.handleNodeL is not None:
            self.handleNodeL.clearHandles()
            self.handleNodeL.addVertices(verts=[]) #TODO
            self.handleNodeL.setOrgToCentroid()
            if self.updateHandleNodeLFrom >= 0:
                self.handleNodeL.setTransform(self.robot_skeleton.bodynodes[self.updateHandleNodeLFrom].T)
            self.handleNodeL.recomputeOffsets()

        if self.handleNodeR is not None:
            self.handleNodeR.clearHandles()
            self.handleNodeR.addVertices(verts=[]) #TODO
            self.handleNodeR.setOrgToCentroid()
            if self.updateHandleNodeRFrom >= 0:
                self.handleNodeR.setTransform(self.robot_skeleton.bodynodes[self.updateHandleNodeRFrom].T)
            self.handleNodeR.recomputeOffsets()

        if self.simulateCloth:
            self.CP0Feature.fitPlane(normhint=np.array([1.0,0,0]))
            self.CP1Feature.fitPlane()
            if self.reset_number == 0:
                self.separatedMesh.initSeparatedMeshGraph()
                self.separatedMesh.updateWeights()
                self.separatedMesh.computeGeodesic(feature=self.CP0Feature, oneSided=True, side=0, normalSide=1)

            if self.limbProgressReward:
                self.limbProgress = pyutils.limbFeatureProgress(limb=pyutils.limbFromNodeSequence(self.robot_skeleton, nodes=self.limbNodesR,offset=self.fingertip), feature=self.CP0Feature)

        a=0

    def extraRenderFunction(self):
        renderUtils.setColor(color=[0.0, 0.0, 0])
        GL.glBegin(GL.GL_LINES)
        GL.glVertex3d(0,0,0)
        GL.glVertex3d(-1,0,0)
        GL.glEnd()

        renderUtils.setColor([0,0,0])
        renderUtils.drawLineStrip(points=[self.robot_skeleton.bodynodes[4].to_world(np.array([0.0,0,-0.075])), self.robot_skeleton.bodynodes[4].to_world(np.array([0.0,-0.3,-0.075]))])
        renderUtils.drawLineStrip(points=[self.robot_skeleton.bodynodes[9].to_world(np.array([0.0,0,-0.075])), self.robot_skeleton.bodynodes[9].to_world(np.array([0.0,-0.3,-0.075]))])

        renderUtils.drawLineStrip(points=[self.robot_skeleton.bodynodes[7].to_world(np.array([0.0, -0.075, 0.0])),
                                          self.prevOracle+self.robot_skeleton.bodynodes[7].to_world(np.array([0.0, -0.075, 0.0])),
                                          ])

        if self.CP0Feature is not None:
            self.CP0Feature.drawProjectionPoly()
        if self.CP1Feature is not None:
            self.CP1Feature.drawProjectionPoly()

        # render geodesic
        '''
        for v in range(self.clothScene.getNumVertices()):
            side1geo = self.separatedMesh.nodes[v + self.separatedMesh.numv].geodesic
            side0geo = self.separatedMesh.nodes[v].geodesic

            pos = self.clothScene.getVertexPos(vid=v)
            norm = self.clothScene.getVertNormal(vid=v)
            renderUtils.setColor(color=renderUtils.heatmapColor(minimum=0, maximum=self.separatedMesh.maxGeo, value=self.separatedMesh.maxGeo-side0geo))
            renderUtils.drawSphere(pos=pos-norm*0.01, rad=0.01)
            renderUtils.setColor(color=renderUtils.heatmapColor(minimum=0, maximum=self.separatedMesh.maxGeo, value=self.separatedMesh.maxGeo-side1geo))
            renderUtils.drawSphere(pos=pos + norm * 0.01, rad=0.01)
        '''

        textHeight = 15
        textLines = 2

        if self.renderUI:
            renderUtils.setColor(color=[0.,0,0])
            self.clothScene.drawText(x=15., y=textLines*textHeight, text="Steps = " + str(self.numSteps), color=(0., 0, 0))
            textLines += 1
            self.clothScene.drawText(x=15., y=textLines*textHeight, text="Reward = " + str(self.reward), color=(0., 0, 0))
            textLines += 1
            self.clothScene.drawText(x=15., y=textLines * textHeight, text="Cumulative Reward = " + str(self.cumulativeReward), color=(0., 0, 0))
            textLines += 1
            self.clothScene.drawText(x=15., y=textLines * textHeight, text="Previous Avg Geodesic = " + str(self.prevAvgGeodesic), color=(0., 0, 0))
            textLines += 1
            self.clothScene.drawText(x=15., y=textLines * textHeight, text="Limb Progress = " + str(self.limbProgress), color=(0., 0, 0))
            textLines += 1

            if self.numSteps > 0:
                renderUtils.renderDofs(robot=self.robot_skeleton, restPose=None, renderRestPose=False)

            renderUtils.drawProgressBar(topLeft=[600, self.viewer.viewport[3] - 12], h=16, w=60, progress=self.limbProgress, color=[0.0, 3.0, 0])
            renderUtils.drawProgressBar(topLeft=[600, self.viewer.viewport[3] - 30], h=16, w=60, progress=-self.previousDeformationReward, color=[1.0, 0.0, 0])
