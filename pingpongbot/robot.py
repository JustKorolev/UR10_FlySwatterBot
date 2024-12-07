import rclpy
import numpy as np
import random

from math import pi, sin, cos, acos, atan2, sqrt, fmod, exp

# Grab the utilities
from pingpongbot.utils.GeneratorNode      import GeneratorNode
from pingpongbot.utils.TransformHelpers   import *
from pingpongbot.utils.TrajectoryUtils    import *

# Grab the general fkin from HW6 P1.
from pingpongbot.utils.KinematicChain     import KinematicChain
 # Import the format for the condition number message
from geometry_msgs.msg import Pose, Vector3, Point

#
# Trajectory Class
#
class Trajectory():
    # Initialization
    def __init__(self, node):

        self.chain = KinematicChain(node, 'world', 'tip', self.jointnames())

        # Our home joint angles
        self.home_q = np.array([-0.1, -2.2, 2.4, -1.0, 1.6, 1.6])
        self.q_centers = np.array([-pi/2, -pi/2, 0, -pi/2, 0, 0])

        # Initial position
        self.q0 = self.home_q
        self.home_p, self.home_R, _, _  = self.chain.fkin(self.home_q)
        self.p0 = self.home_p
        self.R0 = self.home_R

        # Swing back variables
        self.swing_back_time = 3
        self.R_swing_back = R_from_RPY(0, 0, 0)
        self.swing_rot_axis_array, self.swing_rot_angle = axisangle_from_R(self.R_swing_back - self.home_R)
        self.swing_rot_axis = nxyz(self.swing_rot_axis_array[0], self.swing_rot_axis_array[1], self.swing_rot_axis_array[2])
        self.swing_back_q = None

        # Swing variables
        self.hit_time = float("inf") # TODO: SHOULD BE SCALED TO PATH DISTANCE
        self.return_time = float("inf")
        self.hit_pos = np.zeros(3)
        self.hit_rotation = Reye()
        self.hit_q = np.zeros(6)

        self.qd = self.q0
        self.pd = self.p0
        self.Rd = self.R0

        # Tuning constants
        self.lam = 20
        self.lam_second = 15
        self.gamma = 0.2
        self.gamma_array = [self.gamma ** 2] * len(self.jointnames())
        self.joint_weights = np.array([1, 1, 1, 1, 1, 1])
        self.weight_matrix = np.diag(self.joint_weights)

        # Publishing
        self.tip_pose_pub = node.create_publisher(Pose, "/tip_pose", 100)
        self.tip_vel_pub = node.create_publisher(Vector3, "/tip_vel", 100)

        # Subscribing
        node.create_subscription(Point, "/ball_pos", self.ball_pos_callback, 10)

        # Subscription variables
        # Store ball positions and times in a list
        self.ball_trajectory = []
        #We can modify this to whatever we desire, i think 0.5 makese sense
        self.z_target = 0.5 
        self.ball_pos = np.array([0.8, 0.8, 0.8])


    def jointnames(self):
        return ['shoulder_pan_joint', 'shoulder_lift_joint', 'elbow_joint',
                'wrist_1_joint', 'wrist_2_joint', 'wrist_3_joint']

    def get_target_surface_normal(self):
    #Target normal is along the z-axis in the world frame? Double check this
        return nz()

    def get_current_tool_normal(self):
        # get curr orientation matrix from forward kines
        _, R_current, _, _ = self.chain.fkin(self.qd) 
        return R_current[:, 2]  #z-axis of the current rotation matrix

    def compute_rotation_from_normals(self, current_normal, target_normal):
        current_normal = current_normal / np.linalg.norm(current_normal)
        target_normal = target_normal / np.linalg.norm(target_normal)

        # Compute the rotation axis and angle
        rotation_axis = np.cross(current_normal, target_normal)
        rotation_angle = np.arccos(np.clip(np.dot(current_normal, target_normal), -1.0, 1.0))

        # Handle edge cases (normals are parallel or anti-parallel)
        if np.linalg.norm(rotation_axis) < 1e-6:  
            # Parallel normals
            return Reye()
        rotation_axis = rotation_axis / np.linalg.norm(rotation_axis)  
        # Normalize the axis

        # Use Rodrigues formula to compute the rotation matrix
        return rodrigues_formula(rotation_axis, rotation_angle)



    def adjust_jacobian(self, Jv, Jw):
        J_combined = np.vstack((Jv, Jw))

        # get the Surface Normals
        target_normal = self.get_target_surface_normal() 
        current_normal = self.get_current_tool_normal()  
        #Rotation Matrix to Align the Normals
        R_o = self.compute_rotation_from_normals(current_normal, target_normal)
        R_full = np.block([
            [R_o,           np.zeros((3, 3))],  
            [np.zeros((3, 3)),            R_o]]) 
        # Rotate the Jacobian
        J_rotated = R_full @ J_combined  
        # Remove the last row of the Jacobian
        J_adjusted = J_rotated[:-1, :]

        return J_adjusted



    def evaluate(self, t, dt):
        pd = self.pd
        desired_hit_velocity = np.array([2, 2, 2])

        # Swing back sequence
        # if t < self.swing_back_time:
        #     pd, vd = spline(t, self.swing_back_time, self.home_p, swing_back_pd,
        #                     np.zeros(3), np.zeros(3))
        #     self.swing_back_q = self.qd

        # Hit sequence
        if t < self.hit_time:
            # TODO: want to do this only once
            R_rel = self.home_R.T @ self.hit_rotation
            rot_axis, theta = axisangle_from_R(R_rel)
            _, _, Jvf, _ = self.chain.fkin(self.hit_q)
            qdotf = np.linalg.pinv(Jvf) @ desired_hit_velocity
            if t < dt: # TODO: TENTATIVE FIX
                # Testing with random pitch rotation
                Rf = (Rotz(pi) @ Rotx(0)) @ (Roty(0))
                print("Rf (Desired Hit Rotation Matrix):\n", Rf)

                self.hit_rotation = Rf
                print("self.hit_rotation after assignment:\n", self.hit_rotation)
                self.hit_pos = self.ball_pos # TODO: REDUNDANT

                self.hit_q = self.newton_raphson(self.ball_pos, self.home_q)

                self.hit_time = self.calculate_sequence_time(self.qd, self.hit_q, np.zeros(6), qdotf)
                #____________________________________________________


            # ROTATION CALCULATION
            sr, srdot = goto(t, self.hit_time, 0, 1)
            Rot = rodrigues_formula(rot_axis, theta * sr)
            Rd = self.home_R @ Rot
            wd = self.home_R @ rot_axis * (theta * srdot)

            # Pure position manipulation
            qd_hit, qddot_hit = spline(t, self.hit_time, self.home_q, self.hit_q, np.zeros(6), qdotf)

            pd, _, Jv, _ = self.chain.fkin(qd_hit)
            vd = Jv @ qddot_hit

        # Return home sequence
        elif t < self.hit_time + self.return_time:
            if t < self.hit_time + dt: # TODO: TENTATIVE FIX
                self.return_time = 1 # TODO: MAKE DYNAMIC LIKE SELF.HIT_TIME
            #TODO: DO ONLY ONCE
            # _____________________

            R_rel = self.hit_rotation.T @ self.home_R
            rot_axis, theta = axisangle_from_R(R_rel)
            #________________________


            # POSITION CALCULATION
            pd, vd = spline(t - self.hit_time, self.return_time,
                            self.hit_pos, self.home_p, desired_hit_velocity, np.zeros(3))

            # print(pd)
            # print(vd)

            # ROTATION CALCULATION

            sr, srdot = goto(t - self.hit_time, self.return_time, 0, 1)
            Rot = rodrigues_formula(rot_axis, theta * sr)
            Rd = self.hit_rotation @ Rot
            wd = self.hit_rotation @ (rot_axis * theta * srdot)
        else:
            pd = self.pd
            vd = np.zeros(3)
            Rd = self.Rd
            wd = np.zeros(3)



        # TODO: TEMPORARY UNCHANGING ROTATION -- CHANGE
        # Kinematics
        qdlast = self.qd
        pdlast = self.pd
        Rdlast = self.Rd
        pr, Rr, Jv, Jw = self.chain.fkin(qdlast)


        #print("Desired Rotation Matrix:\n", Rd)

        # Position and rotation errors
        error_p = ep(pdlast, pr)
        error_r = eR(Rdlast, Rr)

        # Adjusted velocities
        adjusted_vd = vd + (self.lam * error_p)
        # adjusted_wd = (wd + (self.lam * error_r))[:2]
        adjusted_wd = (wd + (self.lam * error_r))
        combined_vwd = np.concatenate([adjusted_vd, adjusted_wd])

        # Jacobian adjustments
        # J_adjusted = self.adjust_jacobian(Jv, Jw)
        J_adjusted = np.vstack([Jv, Jw])
        J_p = J_adjusted[:3, :]
        J_s = J_adjusted[3:, :]
        J_pinv_p = np.linalg.pinv(J_p)
        J_pinv_s = np.linalg.pinv(J_s)
        J_pinv = np.linalg.pinv(J_adjusted)

        # Primary task
        qddot_main = J_pinv_p @ adjusted_vd

        # Secondary task
        qddot_secondary = J_pinv_s @ adjusted_wd
        N = J_adjusted.shape[1]

        # BASIC QDDOT CALCULATION
        # TODO: CONSIDER USING TARGETED-REMOVAL/BLENDING
        jac_winv = np.linalg.pinv(J_adjusted.T @ J_adjusted +\
                                np.diag(self.gamma_array)) @ J_adjusted.T
        qddot = jac_winv @ combined_vwd

        # QDDOT WITH JOINT WEIGHTING MATRIX
        # qddot = self.weight_matrix @ jac_winv.T @\
        #     np.linalg.pinv(jac_winv @ self.weight_matrix @ jac_winv.T) @ combined_vwd

        # MORE SOPHISTICATED QDDOT CALCULATIONS
        # if not (t < self.hit_time):
        #     qddot = qddot_main + (np.eye(N) - J_pinv_p @ J_p) @ qddot_secondary
        # else:
        #     qddot = J_pinv @ combined_vwd
        #     # qddot = qddot_hit + (np.eye(N) - J_pinv_p @ J_p) @ qddot_secondary

        qd = qdlast + dt * qddot

        # Update state
        self.qd = qd
        self.pd = pd
        self.Rd = Rd

        # Publishing
        self.tip_pose_msg = self.create_pose(self.pd, self.Rd)
        self.tip_vel_msg = self.create_vel_vec(adjusted_vd)
        self.tip_pose_pub.publish(self.tip_pose_msg)
        self.tip_vel_pub.publish(self.tip_vel_msg)


    # Once we have at least two recorded positions of the ball, we can attempt kinematics
        if len(self.ball_trajectory) > 1:
            result = self.time_forZ(self.z_target)
            if result is not None:
                t_hit, x_hit, y_hit = result
                # ddebuggs
                # self.chain.node.get_logger().info(f"At z={self.z_target}, t={t_hit:.3f}s, x={x_hit:.3f}, y={y_hit:.3f}")

        return (self.qd, np.zeros_like(self.qd), self.pd, np.zeros(3), self.Rd, np.zeros(3))
        #For testing
        #return (qd, qddot, pd, vd, Rd, wd)

    # Newton Raphson
    def newton_raphson(self, pgoal, Rgoal, q0):

        # Collect the distance to goal and change in q every step!
        pdistance = []
        qstepsize = []

        # Number of steps to try.
        N = 100

        # Setting initial q
        q = q0

        # IMPLEMENT THE NEWTON-RAPHSON ALGORITHM!
        for _ in range(N):
            (pr, _, Jv, _) = self.chain.fkin(q)
            jac = Jv #np.vstack((Jv, Jw))
            q_new = q + np.linalg.pinv(jac) @ (pgoal - pr)
            qstepsize.append(np.linalg.norm(q_new - q))
            pdistance_curr = np.linalg.norm(pgoal - pr)
            pdistance.append(pdistance_curr)
            q = q_new

        # Unwrap
        for i in range(len(q)):
            q[i] = fmod(q[i], 2*pi)

        return q


    # TODO: MAKE THIS MORE SOPHISTICATED
    def calculate_sequence_time(self, q0, qf, qddot0, qddotf):
        # TODO: THIS IS VERY JANK
        # avg_qddot = np.linalg.norm(qddotf - qddot0) / 4
        # print(np.linalg.norm((qf - q0)) / avg_qddot)
        return np.linalg.norm((qf - q0)) / 3 # TODO NEEDS WORK


    # Takes a numpy array position and R matrix to produce a ROS pose msg
    def create_pose(self, position, orientation):
        pose = Pose()
        pose.position = Point_from_p(position)
        pose.orientation = Quaternion_from_R(orientation)
        return pose

    def create_vel_vec(self, velocity):
        vx, vy, vz = velocity
        vec3 = Vector3(x=vx, y=vy, z=vz)
        return vec3
    

    def ball_pos_callback(self, pos):
        pos_array = np.array([pos.x, pos.y, pos.z])
        # Get current time
        current_time = self.chain.node.get_clock().now().nanoseconds * 1e-9 
        # Store the (time, position)
        self.ball_trajectory.append((current_time, pos_array))
        self.ball_pos = pos_array


    def time_forZ(self, z_target):
        if len(self.ball_trajectory) < 2:
            return None

        #finding the initial conditions
        (t0, p0) = self.ball_trajectory[0]
        (t1, p1) = self.ball_trajectory[1]

        x0, y0, z0 = p0
        x1, y1, z1 = p1
        dt = t1 - t0
        if dt <= 0:
            return None

        #initial velocities
        vx0 = (x1 - x0) / dt
        vy0 = (y1 - y0) / dt
        vz0 = (z1 - z0) / dt

        # Kinematic parameters
        g = 9.81
        a = -0.5 * g
        b = vz0
        c = (z0 - z_target)

        discriminant = b**2 - 4*a*c
        if discriminant < 0:
            # No real solution, ball might never reach that z
            return None

        t_sol1 = (-b + np.sqrt(discriminant)) / (2*a)
        t_sol2 = (-b - np.sqrt(discriminant)) / (2*a)

        # it only makes sense to choose the positiove time so well go with that
        t_candidates = [t for t in [t_sol1, t_sol2] if t > 0]
        if not t_candidates:
            return None
        t_hit = min(t_candidates)
        x_hit = x0 + vx0 * t_hit
        y_hit = y0 + vy0 * t_hit

        return (t_hit, x_hit, y_hit)



def main(args=None):
    rclpy.init(args=args)
    generator = GeneratorNode('generator', 200, Trajectory)
    generator.spin()
    generator.shutdown()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
