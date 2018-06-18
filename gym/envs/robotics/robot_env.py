import os
import copy
import numpy as np

import gym
from gym import error, spaces
from gym.utils import seeding

try:
    import mujoco_py
    from mujoco_py.modder import TextureModder, LightModder, CameraModder

except ImportError as e:
    raise error.DependencyNotInstalled("{}. (HINT: you need to install mujoco_py, and also perform the setup instructions here: https://github.com/openai/mujoco-py/.)".format(e))


class RobotEnv(gym.GoalEnv):
    def __init__(self, model_path, initial_qpos, n_actions, n_substeps):
        if model_path.startswith('/'):
            fullpath = model_path
        else:
            fullpath = os.path.join(os.path.dirname(__file__), 'assets', model_path)
        if not os.path.exists(fullpath):
            raise IOError('File {} does not exist'.format(fullpath))

        print('full path', fullpath)
        model = mujoco_py.load_model_from_path(fullpath)
        self.sim = mujoco_py.MjSim(model, nsubsteps=n_substeps)
        self.text_modder = TextureModder(self.sim)
        self.ligh_modder = LightModder(self.sim)
        self.came_modder = CameraModder(self.sim)
        self.camera_init = self.came_modder.get_pos('external_camera_0').copy()

        self.viewer = None

        self.metadata = {
            'render.modes': ['human', 'rgb_array'],
            'video.frames_per_second': int(np.round(1.0 / self.dt))
        }

        self.seed()
        self._env_setup(initial_qpos=initial_qpos)
        self.initial_state = copy.deepcopy(self.sim.get_state())

        self.goal = self._sample_goal()
        obs = self._get_obs()

        action_val = 1. if n_actions == 4 else np.pi
        self.action_space = spaces.Box(-action_val, action_val, shape=(n_actions,), dtype='float32')
        self.observation_space = spaces.Dict(dict(
            desired_goal=spaces.Box(-np.inf, np.inf, shape=obs['achieved_goal'].shape, dtype='float32'),
            achieved_goal=spaces.Box(-np.inf, np.inf, shape=obs['achieved_goal'].shape, dtype='float32'),
            observation=spaces.Box(-np.inf, np.inf, shape=obs['observation'].shape, dtype='float32'),
        ))

    @property
    def dt(self):
        return self.sim.model.opt.timestep * self.sim.nsubsteps

    # Env methods
    # ----------------------------

    def seed(self, seed=None):
        self.np_random, seed = seeding.np_random(seed)
        return [seed]

    def step(self, action):
        action = np.clip(action, self.action_space.low, self.action_space.high)
        self._set_action(action)
        self.sim.step()
        self._step_callback()
        obs = self._get_obs()

        done = False
        info = {
            'is_success': self._is_success(obs['achieved_goal'], self.goal),
        }
        reward = self.compute_reward(obs['achieved_goal'], self.goal, info)
        return obs, reward, done, info

    def rand_texture(self):
        for name in self.sim.model.geom_names:
            if 'robot0:' not in name:
                if name == 'object0':
                    # random red
                    self.text_modder.rand_rgb(name, rgb=(1, 0, 0))
                else:
                    self.text_modder.rand_all(name)

    def set_light(self):
        light_name = 'light0'
        arm_init_pos = np.array([1.425, 1.333, 0.])
        x, y, z = 2., 0., 3.
        lightpos = np.array([x, y, z]) + arm_init_pos
        # lightdir = np.append(np.random.normal(-0.4, 0.3, size=(1,)), np.random.normal(0., 0.3, size=(1,)))
        # lightdir = np.append(lightdir, np.random.normal(-0.5, 0.15, size=(1,)))
        lightdir = np.append(np.random.uniform(-.9, .1, size=(1,)), np.random.uniform(-.9, .9, size=(1,)))
        lightdir = np.append(lightdir, np.random.uniform(-.9, 0., size=(1,)))
        self.ligh_modder.set_pos(light_name, lightpos)
        self.ligh_modder.set_dir(light_name, lightdir)
        self.ligh_modder.set_castshadow(light_name, True)
        self.ligh_modder.set_ambient(light_name, [0.2, 0.2, 0.2])
        self.ligh_modder.set_diffuse(light_name, [0.7, 0.7, 0.7])
        self.ligh_modder.set_specular(light_name, [0.3, 0.3, 0.3])

    def set_camera(self):
        camera_name = 'external_camera_0'
        y = np.random.normal(0., 0.05, size=(1,))
        pos = np.array([0., y, 0.]) + self.camera_init
        self.came_modder.set_pos(camera_name, pos)

    def reset(self, object_pos=None, rand_text=False, rand_shadow=False, rand_cam=False):
        # Attempt to reset the simulator. Since we randomize initial conditions, it
        # is possible to get into a state with numerical issues (e.g. due to penetration or
        # Gimbel lock) or we may not achieve an initial condition (e.g. an object is within the hand).
        # In this case, we just keep randomizing until we eventually achieve a valid initial
        # configuration.
        if rand_text:
            self.rand_texture()
        if rand_shadow:
            self.set_light()
        if rand_cam:
            self.set_camera()
            
        self.goal = self._sample_goal().copy()
        did_reset_sim = False
        while not did_reset_sim:
            did_reset_sim = self._reset_sim(object_pos)
        
        obs = self._get_obs()
        return obs

    def close(self):
        if self.viewer is not None:
            self.viewer.finish()
            self.viewer = None

    def render(self, mode='human'):
        self._render_callback()
        if mode == 'rgb_array':
            self._get_viewer().render()
            # window size used for old mujoco-py:
            width, height = 500, 500
            data = self._get_viewer().read_pixels(width, height, depth=False)
            # data = self.sim.render(width=width, height=height, camera_name="gripper_camera_rgb", depth=False,
            #    mode='offscreen', device_id=-1)
            # original image is upside-down, so flip it
            return data[::-1, :, :]
        elif mode == 'human':
            self._get_viewer().render()

    def _get_viewer(self):
        if self.viewer is None:
            self.viewer = mujoco_py.MjViewer(self.sim)
            self._viewer_setup()
        return self.viewer

    # Extension methods
    # ----------------------------

    def _reset_sim(self):
        """Resets a simulation and indicates whether or not it was successful.
        If a reset was unsuccessful (e.g. if a randomized state caused an error in the
        simulation), this method should indicate such a failure by returning False.
        In such a case, this method will be called again to attempt a the reset again.
        """
        self.sim.set_state(self.initial_state)
        self.sim.forward()
        return True

    def _get_obs(self):
        """Returns the observation.
        """
        raise NotImplementedError()

    def _set_action(self, action):
        """Applies the given action to the simulation.
        """
        raise NotImplementedError()

    def _is_success(self, achieved_goal, desired_goal):
        """Indicates whether or not the achieved goal successfully achieved the desired goal.
        """
        raise NotImplementedError()

    def _sample_goal(self):
        """Samples a new goal and returns it.
        """
        raise NotImplementedError()

    def _env_setup(self, initial_qpos):
        """Initial configuration of the environment. Can be used to configure initial state
        and extract information from the simulation.
        """
        pass

    def _viewer_setup(self):
        """Initial configuration of the viewer. Can be used to set the camera position,
        for example.
        """
        pass

    def _render_callback(self):
        """A custom callback that is called before rendering. Can be used
        to implement custom visualizations.
        """
        pass

    def _step_callback(self):
        """A custom callback that is called after stepping the simulation. Can be used
        to enforce additional constraints on the simulation state.
        """
        pass
