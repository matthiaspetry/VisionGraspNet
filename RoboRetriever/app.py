from flask import Flask, jsonify, request, render_template,Response,make_response
from flask_cors import CORS,cross_origin
import threading
import time
from rtde_control import RTDEControlInterface as RTDEControl
from rtde_receive import RTDEReceiveInterface as RTDEReceive
import robotiq_gripper
import numpy as np
import cv2
from ultralytics import YOLO
import torch
import torch.nn as nn
import timm
from torchvision import transforms
from PIL import Image
import roboticstoolbox as rtb
import random 
import pickle
import math
import json


app = Flask(__name__)
cors = CORS(app, resources={r"/*": {"origins": "http://localhost:3000"}})




# Shared variables
selected_object = None
selected_model = "None"
selected_speed = None
is_processing_active = False
rtde_r = None
rtde_c = None
rounded_position = None
pickuptime = None
roboConnection = False
gripperConnection = False
objectPickedUp = False
objectPlacePosition = [-1.72233421, -1.57549443,  1.25739509, -1.22777946, -1.61194212, -0.13999015]
objectPlacePositionSet = False
count = 1
mode = False
pickup = False
CrossCount = 0
CubeCount = 0
CylinderCount = 0
HexagonCount = 0
PyramidCount = 0
Y_CubeCount = 0
OBJECTCOUNT = 0
MISSEDCOUNT = 0
OBJECTCOUNTSTATIC = 0
MISSEDSTATIC = 0
OBJECTCOUNTDYNAMIC = 0
MISSEDDYNAMIC = 0
TOTALCOUNTSTATIC = OBJECTCOUNTSTATIC + MISSEDSTATIC
TOTALCOUNTDYNAMIC = OBJECTCOUNTDYNAMIC + MISSEDDYNAMIC
TOTALCOUNT = OBJECTCOUNT + MISSEDCOUNT
completePickTime = 0
velocity = 0.1
acceleration = 0.1
dt = 0.1
lookahead_time = 0.2
gain = 2000
joint_q = [0.0000,-1.5708,-0.0000,-1.5708,-0.0000,0.0000]


#Scaling Variables
mean = -0.02049437658091184
std = 1.4781722524455945


#Set up Robot Toolbox
dh_params = np.array([
    [0, 0.15185,       0,   np.pi/2],
    [0, 0,      -0.24355,         0],
    [0, 0,       -0.2132,         0],
    [0, 0.13105,       0,   np.pi/2],
    [0, 0.08535,       0,  -np.pi/2],
    [0, 0.0921,        0,         0]
])

robot = rtb.DHRobot([
    rtb.RevoluteDH(d=dh_params[i, 1], a=dh_params[i, 2], alpha=dh_params[i, 3]) for i in range(6)
], name='UR3e')

#Scaling of Joint Module output
def reverse_standard_scaling(mean, std, scaled_data):
        original_data = [(val * std) + mean for val in scaled_data]
        return original_data

#Joint Module 
class JointModule(nn.Module):
    def __init__(self, dropout_rate=0.1, vector_input_size=8, intermediate_size=128, hidden_layer_size=64):
        super(JointModule, self).__init__()

        # Load FastViT model pre-trained on ImageNet
        self.fastvit = timm.create_model('fastvit_t8.apple_dist_in1k', pretrained=False)
        in_features = self.fastvit.get_classifier().in_features
        self.fastvit.reset_classifier(num_classes=0)  # Remove the classifier

        # Model for processing 8D vector input with LayerNorm
        self.vector_model = nn.Sequential(
            nn.Linear(vector_input_size, intermediate_size),
            nn.ReLU(),
            nn.LayerNorm(intermediate_size),
            nn.Linear(intermediate_size, in_features),
            nn.ReLU(),
            nn.LayerNorm(in_features),
            nn.Dropout(dropout_rate)
        )

        # Enhanced combined output layer with LayerNorm
        self.combined_output_layer = nn.Sequential(
            nn.Linear(in_features * 2, hidden_layer_size),
            nn.ReLU(),
            nn.LayerNorm(hidden_layer_size),
            nn.Dropout(dropout_rate),
            nn.Linear(hidden_layer_size, hidden_layer_size),
            nn.ReLU(),
            nn.LayerNorm(hidden_layer_size),
            nn.Linear(hidden_layer_size, 6)
        )

    def forward(self, x, vector):
        # Extract features using FastViT
        fastvit_features = self.fastvit(x)

        # Process the 4D vector through the vector model
        vector_features = self.vector_model(vector)

        # Concatenate FastViT and vector features
        concatenated_features = torch.cat((fastvit_features, vector_features), dim=1)

        # Final output layer for regression
        final_output = self.combined_output_layer(concatenated_features)

        return final_output

def main_processing_loop():
    global is_processing_active, rtde_c, rtde_r
    global cap, rounded_position, pickuptime, mode,pickup ,OBJECTCOUNT, MISSEDCOUNT,objectPicked,selected_speed,OBJECTCOUNTSTATIC, MISSEDSTATIC,OBJECTCOUNTDYNAMIC, MISSEDDYNAMIC
    global CrossCount, CubeCount, CylinderCount, HexagonCount, PyramidCount, Y_CubeCount,completePickTime, velocity,acceleration,dt,lookahead_time,gain,joint_q
    #Load YOLO model
    model = YOLO("yolov8s.yaml") 
    model = YOLO("models/best.pt") 

    #Load Joint Module model
    joint_model = JointModule()
    state_dict = torch.load("models/fastvit_t8_114_2.pth",map_location=torch.device('cpu'))
    joint_model.load_state_dict(state_dict)
    joint_model.to("cpu")
    joint_model.eval()
    
    #Load Decision Module model
    model_name = "resnet10t.c3_in1k" 
    model2 = timm.create_model(model_name, pretrained=False, num_classes=2)
    state_dict2 = torch.load("models/binary_classification.pth",map_location=torch.device('cpu'))
    model2.load_state_dict(state_dict2)
    model2.to("cpu")
    model2.eval()

    #Camera input for model 
    cap = cv2.VideoCapture(0)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 360)

    if not cap.isOpened():
        print("Cannot open camera")
        exit() 


    #Flags
    detected = False
    grabcount = 0
    startTime = None

    
    #Main Processing
    while True:
        #Static Mode
        if mode == False:
            if is_processing_active:
                
                if not gripper:
                    print("Error: gripper is not initialized. Please connect to the gripper before using it.")
                    return

                # Check if rtde_c is initialized
                if not rtde_c:
                    print("Error: rtde_c is not initialized. Please connect to the robot before using it.")
                    return

                if not rtde_r:
                    print("Error: rtde_r is not initialized. Please connect to the robot before using it.")
                    return

                
                if not selected_object:
                    print(selected_object)
                
                if pickup:
                    #read camera input
                    ret, frame = cap.read()

                    if not ret:
                        print("Can't receive frame (stream end?). Exiting ...")
                        break
                    #Resize for YOLO input
                    resized_frame = cv2.resize(frame, (512,288))
                    #Detect Objects
                    results = model.predict(resized_frame, verbose=False, imgsz=512,device = "cpu")

                    for result in results:
                        boxes = result.boxes.xywhn.tolist()
                        classes = result.boxes.cls.tolist()

                        
                        for i, cls in enumerate(classes):

                            #Select specific object
                            if cls == int(selected_object):

                                #Extract bounding boxes
                                xn, yn, wn, hn = boxes[i]
                                bbox_tensor = torch.tensor([xn, yn, wn, hn])


                                #Transform bounding boxes to xyxyn
                                x1 = xn - (wn / 2)
                                y1 = yn - (hn / 2)
                                x2 = xn + (wn / 2)
                                y2 = yn + (hn / 2)

                                
                                bbox_xyxyn = torch.tensor([x1, y1, x2, y2], dtype=torch.float32)


                                #Combine both bounding box formats
                                bboxs = torch.cat((bbox_tensor, bbox_xyxyn), 0).unsqueeze(0)

                                #Target size for Joint Module
                                target_sizeJM = (256, 256)
                                target_sizeDM = (224,224)

                                normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
                                to_tensor = transforms.ToTensor()



                                frame_np = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                                resizedframeJM = cv2.resize(frame_np, target_sizeJM)
                                resized_imageDM = cv2.resize(frame_np, target_sizeDM)


                                img_pil = Image.fromarray(resizedframeJM)
                                img_tensorJM = to_tensor(img_pil)

                                img_pilDM = Image.fromarray(resized_imageDM)
                                img_tensorDM = to_tensor(img_pilDM)



                                # Start the timer for normalization
                            
                                img_normalized = normalize(img_tensorJM)
                                img_JM = img_normalized.unsqueeze(0)

       
                                img_normalized2 = normalize(img_tensorDM)
                                img_DM = img_normalized2.unsqueeze(0)



                                with torch.no_grad():
                
                                    outputs = joint_model(img_JM, bboxs)
                                    
                                    # Reverse scaling to get the actual joint positions
                                    
                                    jp = reverse_standard_scaling(mean, std, outputs.numpy())[0]
                                    between_jp= reverse_standard_scaling(mean, std, outputs.numpy())[0]
                                    between_jp2 = reverse_standard_scaling(mean, std, outputs.numpy())[0]
                                    between_jp[1] -= 5 * (math.pi / 180)
                                    between_jp[2] -= 5 * (math.pi / 180)# Increase joint 2 by 10 degrees (converted to radians)
                                    between_jp[3] += 5 * (math.pi / 180)
                                    between_jp2[1] -= 10 * (math.pi / 180)
                                    rtde_c.moveJ(between_jp, 3, 3)

                                    rtde_c.moveJ(jp, 1, 1)
                                    rounded_position = [round(p, 4) for p in jp]
                                    gripper.move_and_wait_for_pos(255, 255, 255)
                                    gp = gripper.get_current_position()
                                    if gp < 248:
                                        rtde_c.moveJ(between_jp2,1, 1)
                                        objectPickedUp = True
                                        rtde_c.moveJ([-1.72233421, -1.57549443,  1.25739509, -1.22777946, -1.61194212, -0.13999015],3, 3)
                                        gripper.move_and_wait_for_pos(0, 255, 255)
                                        objectPickedUp = False
                                        rtde_c.moveJ(joint_q,3, 3)
                                        pickup = False
                                        if int(selected_object) == 0:
                                            CrossCount += 1
                                        elif int(selected_object) == 1:
                                            CubeCount += 1
                                        elif int(selected_object) == 2:
                                            CylinderCount += 1
                                        elif int(selected_object) == 3:
                                            HexagonCount += 1
                                        elif int(selected_object) == 4: 
                                            PyramidCount += 1
                                        elif int(selected_object) == 5:
                                            Y_CubeCount += 1

                                        OBJECTCOUNT += 1
                                        OBJECTCOUNTSTATIC += 1
                                    else:
                                        rtde_c.moveJ(joint_q,3, 3)
                                        gripper.move_and_wait_for_pos(0, 255, 255)
                                        time.sleep(5)
                                        MISSEDCOUNT += 1
                                        MISSEDSTATIC += 1


                                
                                    
    
        #Dynamic Mode
        elif mode == True: 
            
           
            if is_processing_active:

                if not gripper:
                    print("Error: gripper is not initialized. Please connect to the gripper before using it.")
                    return

                # Check if rtde_c is initialized
                if not rtde_c:
                    print("Error: rtde_c is not initialized. Please connect to the robot before using it.")
                    return

                if not rtde_r:
                    print("Error: rtde_r is not initialized. Please connect to the robot before using it.")
                    return

                
                if not selected_object:
                    print(selected_object)
                
                t_start = rtde_c.initPeriod()
                ret, frame = cap.read()
                if not ret:
                    print("Can't receive frame (stream end?). Exiting ...")
                    break
                #Resize for YOLO input
                resized_frame = cv2.resize(frame, (512,288))
                #Detect Objects
                results = model.predict(resized_frame, verbose=False, imgsz=512,conf=0.7)


                for result in results:
                    boxes = result.boxes.xywhn.tolist()
                    classes = result.boxes.cls.tolist()
                    
                    for i, cls in enumerate(classes):
                
                        
                            
                        #Select specific object
                        if cls == int(selected_object):
                            if detected == False:
                                detected = True
                                startTime = time.time()
                                print(f"Start Time: {startTime}")
                            
                            
                                #Extract bounding boxes
                                xn, yn, wn, hn = boxes[i]
                                bbox_tensor = torch.tensor([xn, yn, wn, hn])


                                #Transform bounding boxes to xyxyn
                                x1 = xn - (wn / 2)
                                y1 = yn - (hn / 2)
                                x2 = xn + (wn / 2)
                                y2 = yn + (hn / 2)

                                
                                bbox_xyxyn = torch.tensor([x1, y1, x2, y2], dtype=torch.float32)


                                #Combine both bounding box formats
                                bboxs = torch.cat((bbox_tensor, bbox_xyxyn), 0).unsqueeze(0)

                                #Target size for Joint Module
                                target_sizeJM = (256, 256)
                                target_sizeDM = (224,224)

                                normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
                                to_tensor = transforms.ToTensor()



                                frame_np = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                                resizedframeJM = cv2.resize(frame_np, target_sizeJM)
                                resized_imageDM = cv2.resize(frame_np, target_sizeDM)


                                img_pil = Image.fromarray(resizedframeJM)
                                img_tensorJM = to_tensor(img_pil)

                                img_pilDM = Image.fromarray(resized_imageDM)
                                img_tensorDM = to_tensor(img_pilDM)

                            
                                img_normalized = normalize(img_tensorJM)
                                img_JM = img_normalized.unsqueeze(0)

       
                                img_normalized2 = normalize(img_tensorDM)
                                img_DM = img_normalized2.unsqueeze(0)

                            with torch.no_grad():
                               
                
                                outputs = joint_model(img_JM, bboxs)
                                
                                # Reverse scaling to get the actual joint positions
                                jp = reverse_standard_scaling(mean, std, outputs.numpy())[0]
                                #Forward Kinematics for 3D Pose
                                end_effector_pose = robot.fkine(jp)

                                current_position = rtde_r.getActualQ()
                               

                                #Modeling of the Path
                                if end_effector_pose.t[2] > 0.50:
                                    a = rtb.mtraj(rtb.quintic, current_position, jp, 100)                                   
                                    for position in a.q:
                                        #Real-Time updates to Robot
                                        rtde_c.servoJ(position, velocity, acceleration, dt, lookahead_time, gain)      
                                else:
                                    #Speed Compensation 
                                    end_effector_pose.t[0] = end_effector_pose.t[0] - (int(selected_speed)* 0.01)
                                    end_effector_pose.t[1] = end_effector_pose.t[1]
                                    #Inverse Kinematics
                                    sol = robot.ikine_LM(end_effector_pose,jp)
                                    jp = sol.q
                                    
                                    #Real-Time updates to Robot
                                    rtde_c.servoJ(jp, velocity, acceleration, dt, lookahead_time, gain)
                                        
                                    #Decision Model makes prediction 
                                    pred = model2(img_DM)
                                    grab = torch.argmax(pred, dim=1).numpy()
                                    if grab[0] == 0:
                                        
                                        grabcount += 1
                                    #if 3 predictions grasp process stars
                                    if grab[0] == 0 and grabcount == 2:
                                        
                                        #Grasp
                                        gripper.move_and_wait_for_pos(255, 255, 255)
                                        gp = gripper.get_current_position()

                                        if gp < 248:
                                            objectPickedUp = True
                                    
                                            position = rtde_r.getActualQ()
                                            pickuptime = time.time() - startTime
                                            completePickTime += pickuptime

        
                                            rounded_position = [round(p, 4) for p in position]
                                        
                                            position[1] -= 10 * (math.pi / 180)
                                            rtde_c.servoStop()
                                            rtde_c.moveJ(position,3,3)
                                            del position
                                            
                                        
                                            
                                            detected = False
                                            startTime = None

                                            rtde_c.moveJ([-1.72233421, -1.57549443,  1.25739509, -1.22777946, -1.61194212, -0.13999015],3,3)
                                            #Open Gripper                                           
                                            gripper.move_and_wait_for_pos(0, 255, 255)
                                            #Move to Base Position
                                            rtde_c.moveJ(joint_q,3,3)
                                            grabcount = 0
                                            addcount = 1
                                            #Update Counters
                                            if int(selected_object) == 0:
                                                CrossCount += 1
                                            elif int(selected_object) == 1:
                                                CubeCount += 1
                                            elif int(selected_object) == 2:
                                                CylinderCount += 1
                                            elif int(selected_object) == 3:
                                                HexagonCount += 1
                                            elif int(selected_object) == 4: 
                                                PyramidCount += 1
                                            elif int(selected_object) == 5:
                                                Y_CubeCount += 1

                                            objectPickedUp = False 
                                            OBJECTCOUNT += 1
                                            OBJECTCOUNTDYNAMIC +=1 
                                           
                                        
                                         

                                        else:
                                            startTime = None
                                            detected = False
                                            startTime = None
                                            MISSEDCOUNT += 1
                                            MISSEDDYNAMIC += 1

                                            gripper.move_and_wait_for_pos(0, 255, 255)
                                            rtde_c.servoStop()
                                            rtde_c.moveJ(joint_q,3,3)
                                            grabcount = 0
                                            time.sleep(5)

                                        
                                     
                                
                                rtde_c.waitPeriod(t_start)

                                if cv2.waitKey(1) == ord('q'):
                                    rtde_c.servoStop()
                                    rtde_c.stopScript()
                                    cap.release()
                                    cv2.destroyAllWindows()
                                
    
                                    break

#Seconf Camera Angle 
cap2 = cv2.VideoCapture(1)
cap2.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
cap2.set(cv2.CAP_PROP_FRAME_HEIGHT, 360)

def gen_frames(cap2):  
    while True:
        success, frame = cap2.read()  # read the camera frame
        if not success:
            break
        else:
            ret, buffer = cv2.imencode('.jpg', frame)
            frame = buffer.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
            time.sleep(0.067)


#Camera Feed Route
@app.route('/video_feed')
def video_feed():
    return Response(gen_frames(cap2), mimetype='multipart/x-mixed-replace; boundary=frame')
            

#Start Processing Route
@app.route('/start_processing', methods=['POST'])
def start_processing():
    global is_processing_active
    is_processing_active = True
    return jsonify({'message': 'Processing loop started'})

#Stop Processing Route 
@app.route('/stop_processing', methods=['POST'])
def stop_processing():
    global is_processing_active
    global rtde_c
    
    is_processing_active = False
    return jsonify({'message': 'Processing loop stopped'})

#Select Object Route
@app.route('/select_object', methods=['POST'])
def select_object():
    global selected_object
    data = request.json
    selected_object = data.get('object_type')
    #print(selected_object)
    return jsonify({'message': f'Selected object set to {selected_object}'})

#Select Speed Routw
@app.route('/select_speed', methods=['POST'])
def select_speed():
    global selected_speed
    data = request.json
    selected_speed = data.get('object_type')
    #print(selected_speed)
    return jsonify({'message': f'Selected speed set to {selected_speed}'})

#Select Model
@app.route('/select_model', methods=['POST'])
def select_model():
    global selected_model
    data = request.json
    selected_model = data.get('object_type')
    #print(selected_model)
    return jsonify({'message': f'Selected model set to {selected_model}'})

#Connect to Robot
@app.route('/connect_robot', methods=['POST'])
def connect_robot():
    ROBOT_IP ="192.168.188.32"
    global rtde_c
    global rtde_r
    global gripper
    rtde_c = RTDEControl(ROBOT_IP)
    rtde_r = RTDEReceive(ROBOT_IP)

    def log_info(gripper):
        print(f"Pos: {str(gripper.get_current_position()): >3}  "
            f"Open: {gripper.is_open(): <2}  "
            f"Closed: {gripper.is_closed(): <2}  ")

    print("Creating gripper...")
    gripper = robotiq_gripper.RobotiqGripper()
    print("Connecting to gripper...")
    gripper.connect(ROBOT_IP, 63352)
    print("Activating gripper...")
    gripper.activate()
    global roboConnection
    roboConnection = True
    global gripperConnection
    gripperConnection = True
    return jsonify({'message': f'Robot connected'})

#Move to Base Route
@app.route('/move_2_base', methods=['POST'])
def move_2_base():
    rtde_c.moveJ([0.0000,-1.5708,-0.0000,-1.5708,-0.0000,0.0000])

    return jsonify({'message': f'Moved to base'})

#Set Drop of Positon Route 
@app.route('/set_position', methods=['POST'])
def set_position():
    rtde_c.endFreedriveMode()
    global objectPlacePosition
    global objectPlacePositionSet
    objectPlacePosition = rtde_r.getActualQ()
    objectPlacePositionSet = True
    global is_processing_active
    is_processing_active = False

    return jsonify({'message': f'Position Set'})

#Start Free Mode 
@app.route('/free_mode', methods=['POST'])
def free_mode():
    global is_processing_active
    is_processing_active = "FreeDrive"
    rtde_c.freedriveMode()
    return jsonify({'message': f'freedrive'})

#Pick up Object
@app.route('/pickupObject', methods=['POST'])
def pickupObject():
    global pickup
    pickup = True
    return jsonify({'message': f'pickup'})

#Dynmaic/ Static mode
@app.route('/change_mode', methods=['POST'])
def change_mode():
    global mode
    data = request.get_json()

    if data is None or 'isDynamic' not in data:
        return jsonify({'error': 'Invalid request payload'}), 400

    dynamic_state = data['isDynamic']
    #print(f"Dynamic state updated to: {dynamic_state}")
    mode = dynamic_state


    return jsonify({'message': 'Dynamic state updated successfully'}), 200

#Data Routes
@app.route('/dataTime')
def dataTime():
    global count
    global pickuptime
    #print(pickuptime)

    if pickuptime is not None :  # Check if rounded_position is not None
        try:
            data = {'name': str(count), 'Time': pickuptime}
            pickuptime = None
            count += 1
            
            return jsonify(data)
        except Exception as e:  # Handle potential exceptions during FKine calculation
            print(f"Error calculating end effector pose: {e}")
            return jsonify({'error': 'Error calculating position'}), 500  # Return error message with status code 500
    
    return jsonify({'message': 'No pick up time  available'}), 204  # Return empty response with status code 204 (No Content)

@app.route('/dataPosition')
def dataPosition():
    global rounded_position

    if rounded_position is not None:  # Check if rounded_position is not None
        try:
            end_effector_pose = robot.fkine(rounded_position)
            data = {'x': end_effector_pose.t[0], 'y': end_effector_pose.t[1]}
            return jsonify(data)
        except Exception as e:  # Handle potential exceptions during FKine calculation
            print(f"Error calculating end effector pose: {e}")
            return jsonify({'error': 'Error calculating position'}), 500  # Return error message with status code 500

    return jsonify({'message': 'No position data available'}), 204  # Return empty response with status code 204 (No Content)

@app.route('/dataCount')
def dataCount():
    global CrossCount, CubeCount, CylinderCount, HexagonCount, PyramidCount, Y_CubeCount

    data = [
        {
        'name': 'Cross',
        'Count': CrossCount,
        },
        {
        'name': 'Cube',
        'Count': CubeCount,
        },
        {
        'name': 'Cylinder',
        'Count': CylinderCount,
        },
        {
        'name': 'Hexagon',
        'Count': HexagonCount,
        },
        {
        'name': 'Pyramid',
        'Count': PyramidCount,
        },
        {
        'name': 'Y_Cube',
        'Count': Y_CubeCount,
        }
    ]
    #print(data)
    response = make_response(jsonify(data))
    response.headers["Content-Type"] = "application/json"
    return response



#Status Route
@app.route('/status',methods=["GET"])
def status():
 
    # Example data, replace with your actual data source
    if selected_object == None:
        selected = None
    else:
        selected = int(selected_object)

    if selected == 0:
        obj = "Cross"
    elif selected == 1:
        obj = "Cube"
    elif selected == 2:
        obj = "Cylinder"
    elif selected == 3:
        obj = "Hexagon"
    elif selected == 4:
        obj = "Pyramid"
    elif selected == 5:
        obj = "Y_Cube"
    elif selected == None:
        obj = "Not Selected"

    if mode == True:

        if selected_speed == None:
            speedstr = "Not Selected"

        elif int(selected_speed) == 4:
            speedstr = "66 mm/s"
        elif int(selected_speed) == 6:
            speedstr = "120 mm/s"
        elif int(selected_speed) == 8:
            speedstr = "150 mm/s"
        elif int(selected_speed) == 10:
            speedstr = "200 mm/s"

    else:
        speedstr = "Not Selected"


    data = {
        'isConnected': roboConnection,
        'gripperConnection': gripperConnection,
        'operationalStatus': is_processing_active,
        'currentTask': obj,
        'errorStatus': 'No Errors',
        "objectPickedUp": objectPickedUp,
        "objectPlacePosition": objectPlacePositionSet,
        "selectedModel": selected_model,
        "selectedSpeed": speedstr,
        
    }
    

    response = make_response(jsonify(data))
    response.headers["Content-Type"] = "application/json"
    #print(response)
    return response

#Stats Route 
@app.route("/stats",methods=["GET"])
def stats():
    global MISSEDCOUNT,MISSEDDYNAMIC,MISSEDSTATIC
    global OBJECTCOUNT, OBJECTCOUNTDYNAMIC,OBJECTCOUNTSTATIC,completePickTime

    totalcountstatic = OBJECTCOUNTSTATIC + MISSEDSTATIC
    totalcountdynamic = OBJECTCOUNTDYNAMIC + MISSEDDYNAMIC
    totalcount = OBJECTCOUNT + MISSEDCOUNT


    if totalcount == 0:
        data = {
            "totalCount": totalcount,
            "successRateDynamic": 0,
            "successRateStatic": 0,
            "avgGraspTime": 0,
            "totalSuccessRate": 0
        }

    elif totalcountdynamic == 0:
        data = {
            "totalCount": totalcount,
            "successRateDynamic": 0,
            "successRateStatic":(OBJECTCOUNTSTATIC/totalcountstatic)*100 ,
            "avgGraspTime": 0,
            "totalSuccessRate":(OBJECTCOUNT/totalcount) * 100,
        }
    elif totalcountstatic == 0:
        data = {
            "totalCount": totalcount,
            "successRateDynamic": (OBJECTCOUNTDYNAMIC/totalcountdynamic) * 100,
            "successRateStatic": 0,
            "avgGraspTime": completePickTime/OBJECTCOUNTDYNAMIC,
            "totalSuccessRate": (OBJECTCOUNT/totalcount) * 100,
        }

    else:
        data = {
            "totalCount": totalcount,
            "successRateDynamic": (OBJECTCOUNTSTATIC/totalcountstatic)*100 ,
            "successRateStatic": (OBJECTCOUNTDYNAMIC/totalcountdynamic) * 100,
            "avgGraspTime": completePickTime/OBJECTCOUNTDYNAMIC,
            "totalSuccessRate":(OBJECTCOUNT/totalcount) * 100,
        }

    response = make_response(jsonify(data))
    response.headers["Content-Type"] = "application/json"
    return response

    





@app.route('/')
def index():
    return render_template('index.html')

processing_thread = threading.Thread(target=main_processing_loop)
processing_thread.daemon = True
processing_thread.start()

if __name__ == '__main__':
    app.run(debug=True, threaded=True)
