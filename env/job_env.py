import copy
import logging
import os
from typing import Union, Tuple

import gym
import numpy as np
import datetime
import plotly.figure_factory as ff
import plotly.express as px
from plotly.offline import plot
from gym.core import ActType, ObsType
from matplotlib import animation
from matplotlib.animation import FFMpegWriter

from env import action_space_builder

import matplotlib.pyplot as plt


class JobEnv(gym.Env):
    def __init__(self, args, job_size, machine_size):
        self.args = args
        self.instance = None
        self.job_size = None
        self.machine_size = None
        self.job_machine_nos = None
        self.initial_process_time_channel = None
        self.max_process_time = None

        self.last_process_time_channel = None
        self.last_schedule_finish_channel = None

        self.observation_space = gym.spaces.Box(low=0, high=1, shape=(3, job_size, machine_size))
        self.action_space = gym.spaces.Discrete(18)
        self.action_choices = None

        self.u_t = None
        self.make_span = 0
        self.machine_finish_time_arr = None
        # 用于记录
        self.episode_count = 0
        self.step_count = 0
        self.phase = None
        # 用于记录绘图
        self.cell_colors = None
        self.history_i_j = None
        self.history_machine_finish_time_arr = []
        self.history_make_span = []
        self.history_process_time_channel = []
        self.history_schedule_finish_channel = []
        self.history_machine_utilization_channel = []

    def init_data(self, **kwargs):
        self.instance = kwargs.get("instance")
        self.job_size = self.instance.job_size
        self.machine_size = self.instance.machine_size
        self.job_machine_nos = self.instance.machine_nos
        self.initial_process_time_channel = self.instance.processing_time
        self.max_process_time = np.max(self.instance.processing_time)

        self.action_choices = action_space_builder.build_action_choices(self.instance.processing_time)

        self.u_t = 0
        self.make_span = 0
        self.machine_finish_time_arr = np.zeros(self.machine_size, dtype=np.int32)
        # 用于记录
        self.episode_count = kwargs.get("episode") if "episode" in kwargs else 0
        self.phase = kwargs.get("phase") if "phase" in kwargs else "train"
        self.step_count = 0
        # 用于记录绘图
        self.cell_colors = self.build_cell_colors()
        self.history_i_j = []
        self.history_machine_finish_time_arr = []
        self.history_make_span = []
        self.history_process_time_channel = []
        self.history_schedule_finish_channel = []
        self.history_machine_utilization_channel = []

        setup_time_arr = [
            [5, 6, 7, 8, 9, 10, 11, 12, 13, 14],
            [5, 6, 7, 8, 9, 10, 11, 12, 13, 14],
            [5, 6, 7, 8, 9, 10, 11, 12, 13, 14],
            [10, 11, 10, 15, 9, 20, 15, 12, 17, 20],
            [10, 11, 10, 15, 9, 20, 15, 12, 17, 20],
            [10, 11, 10, 15, 9, 20, 15, 12, 17, 20],
            [7, 14, 12, 13, 14, 14, 14, 14, 14, 17],
            [7, 14, 12, 13, 14, 14, 14, 14, 14, 17],
            [7, 14, 12, 13, 14, 14, 14, 14, 14, 17],
            [7, 14, 12, 13, 14, 14, 14, 14, 14, 17]
        ]
        self.job_machine_setup_time = np.array(setup_time_arr)
        
        for i in range(self.job_size):
            for j in range(self.machine_size):
                self.initial_process_time_channel[i, j] += self.job_machine_setup_time[i, self.job_machine_nos[i, j]]
        self.max_process_time = np.max(self.instance.processing_time)

    def reset(self, **kwargs) -> Union[ObsType, Tuple[ObsType, dict]]:
        self.init_data(**kwargs)
        # 处理时间
        process_time_channel = copy.deepcopy(self.initial_process_time_channel)
        # 调度完成时
        schedule_finish_channel = np.zeros_like(process_time_channel)
        # 机器利用率
        machine_utilization_channel = np.zeros_like(process_time_channel)
        obs = self.get_obs(process_time_channel, schedule_finish_channel, machine_utilization_channel)
        self.add_data_for_visualization(
            process_time_channel, schedule_finish_channel, machine_utilization_channel, None, None
        )
        return obs

    def step(
        self, action: ActType
    ) -> Union[Tuple[ObsType, float, bool, bool, dict], Tuple[ObsType, float, bool, dict]]:
        # logging.info("动作选择: {}".format(action))
        self.step_count += 1
        rule = self.action_choices[action]
        i, j = rule(
            self.last_process_time_channel,
            make_span=self.make_span,
            machine_nos=self.job_machine_nos,
            machine_times=self.machine_finish_time_arr,
        )

        process_time_channel = copy.deepcopy(self.last_process_time_channel)
        process_time_channel[i, j] = 0
        schedule_finish_channel = self.compute_schedule_finish_channel(i, j)
        machine_utilization_channel = self.compute_machine_utilization(process_time_channel)

        obs = self.get_obs(process_time_channel, schedule_finish_channel, machine_utilization_channel)
        reward = self.compute_reward(process_time_channel)
        done = np.sum(process_time_channel) == 0

        if done:
            self.draw_gantt()

        self.add_data_for_visualization(
            process_time_channel, schedule_finish_channel, machine_utilization_channel, i, j
        )
        return obs, reward, done, {}

    def get_obs(self, process_time_channel, schedule_finish_channel, machine_utilization_channel):
        obs = np.array(
            [
                self.normalize_process_time_channel(process_time_channel),
                self.normalize_schedule_finish_channel(schedule_finish_channel),
                machine_utilization_channel,
            ],
            dtype=np.float32,
        )
        # obs = obs.swapaxes(0, 2)
        self.last_process_time_channel = process_time_channel
        self.last_schedule_finish_channel = schedule_finish_channel
        return obs

    def compute_reward(self, process_time_channel):
        # u_t = self.total_working_time / (self.machine_size * self.make_span)
        # 总working time可能指的是当前处理的所有operation的总时间
        u_t = np.sum(self.initial_process_time_channel - process_time_channel) / (self.machine_size * self.make_span)
        reward = u_t - self.u_t
        self.u_t = u_t
        return reward

    def compute_schedule_finish_channel(self, i, j):
        schedule_finish_channel = copy.deepcopy(self.last_schedule_finish_channel)
        if j == 0:
            # 处于某个job第一个operation位置，只需要关注机器时间
            schedule_finish_channel[i, j] = (
                self.initial_process_time_channel[i, j] + self.machine_finish_time_arr[self.job_machine_nos[i, j]]
            )
        else:
            # 对比上一个操作完成时间和对应机器时间，取大的
            schedule_finish_channel[i, j] = self.initial_process_time_channel[i, j] + max(
                self.machine_finish_time_arr[self.job_machine_nos[i, j]], schedule_finish_channel[i, j - 1]
            )
        # 更新机器完成时间(某个作业在该机器上的完成时间即为该机器到目前位置的完成时间)
        self.machine_finish_time_arr[self.job_machine_nos[i, j]] = schedule_finish_channel[i, j]
        # 更新机器完成周期
        self.make_span = np.max(self.machine_finish_time_arr)

        return schedule_finish_channel

    def normalize_process_time_channel(self, process_time_channel):
        return process_time_channel / self.max_process_time

    @staticmethod
    def normalize_schedule_finish_channel(schedule_finish_channel):
        maxes = np.max(schedule_finish_channel, axis=1)
        make_span = np.max(maxes)
        return schedule_finish_channel / make_span if make_span != 0 else schedule_finish_channel

    def compute_machine_utilization(self, process_time_channel):
        # 计算该operation在该机器中所用时间占比
        job_inds = np.argwhere(process_time_channel == 0)
        machine_finish_time_table = np.zeros_like(process_time_channel)
        machine_finish_time_table[job_inds[:, 0], job_inds[:, 1]] = self.machine_finish_time_arr[
            self.job_machine_nos[job_inds[:, 0], job_inds[:, 1]]
        ]
        return machine_finish_time_table / self.make_span

    def add_data_for_visualization(
        self, process_time_channel, schedule_finish_channel, machine_utilization_channel, i, j
    ):
        if not self.args.render:
            return
        self.history_machine_finish_time_arr.append(copy.deepcopy(self.machine_finish_time_arr))
        self.history_make_span.append(self.make_span)
        self.history_process_time_channel.append(copy.deepcopy(process_time_channel))
        self.history_schedule_finish_channel.append(copy.deepcopy(schedule_finish_channel))
        self.history_machine_utilization_channel.append(
            np.around(copy.deepcopy(machine_utilization_channel), decimals=2)
        )
        self.history_i_j.append([i, j])

    def render(self, mode=None):
        if mode is None:
            mode = self.args.mode
        folder = os.path.join(self.args.output, "render")
        os.makedirs(folder, exist_ok=True)

        plt.clf()
        fig = plt.figure(figsize=(16, 8))
        plt.rcParams["font.sans-serif"] = ["SimHei"]  # 设置字体
        plt.rcParams["axes.unicode_minus"] = False  # 该语句解决图像中的“-”负号的乱码问题

        cell_colors = self.cell_colors

        def update(frame_ind):
            plt.clf()
            i, j = self.history_i_j[frame_ind]
            # for _, (i, j) in enumerate(self.history_i_j):
            colors = ["#ffffff" for _ in range(self.machine_size)]
            if i is not None and j is not None:
                cell_colors[i][j] = "#ff0521"
                if frame_ind > 1:
                    cell_colors[self.history_i_j[frame_ind - 1][0]][self.history_i_j[frame_ind - 1][1]] = "#B4EEB4"
                colors[self.job_machine_nos[i, j]] = "#ff0521"
            ax11 = fig.add_subplot(2, 4, 1)
            ax11.set_title("Operation time")
            ax11.table(cellText=self.initial_process_time_channel, loc="center", cellColours=cell_colors)
            ax11.axis("off")

            ax12 = fig.add_subplot(2, 4, 2)
            ax12.set_title("machine number")
            ax12.table(cellText=self.job_machine_nos, loc="center", cellColours=cell_colors)
            ax12.axis("off")

            ax13 = fig.add_subplot(2, 4, 3)

            ax13.set_title("machine finish time")
            ax13.table(cellText=[self.history_machine_finish_time_arr[frame_ind]], loc="center", cellColours=[colors])
            ax13.axis("off")

            ax14 = fig.add_subplot(2, 4, 4)
            ax14.set_title("make span")
            ax14.plot(range(frame_ind + 1), self.history_make_span[: frame_ind + 1])

            ax21 = fig.add_subplot(2, 4, 5)
            ax21.set_title("processing time")
            ax21.table(cellText=self.history_process_time_channel[frame_ind], loc="center", cellColours=cell_colors)
            ax21.axis("off")

            ax22 = fig.add_subplot(2, 4, 6)
            ax22.set_title("schedule finish")
            ax22.table(cellText=self.history_schedule_finish_channel[frame_ind], loc="center", cellColours=cell_colors)
            ax22.axis("off")

            ax23 = fig.add_subplot(2, 4, 7)
            ax23.set_title("machine utilization")
            ax23.table(
                cellText=self.history_machine_utilization_channel[frame_ind], loc="center", cellColours=cell_colors
            )
            ax23.axis("off")
            if mode == "img":
                plt.savefig(
                    os.path.join(folder, "e_{}_step_{}.png".format(self.episode_count, frame_ind)),
                    bbox_inches="tight",
                    pad_inches=0.5,
                    dpi=400,
                )
                plt.clf()

        if mode == "img":
            for ind in range(len(self.history_i_j)):
                update(ind)
            plt.close()
        elif mode == "video":
            out_path = os.path.join(folder, "e_{}.mp4".format(self.episode_count))
            plt.rcParams["animation.ffmpeg_path"] = self.args.ffmpeg
            anim = animation.FuncAnimation(
                fig, update, frames=len(self.history_i_j), interval=len(self.history_i_j) * 2000
            )
            anim.running = True
            ffmpeg_writer = animation.writers["ffmpeg"]
            writer = ffmpeg_writer(fps=10, metadata=dict(artist="Me"), bitrate=1800)
            # writer = animation.FFMpegWriter(fps=10, extra_args=["-vcodec", "libx264"])
            anim.save(out_path, writer=writer)
            # writer.finish()
            # plt.show()

    def build_cell_colors(self):
        cell_colors = []
        for i in range(self.job_size):
            colors = []
            for j in range(self.machine_size):
                colors.append("#ffffff")
            cell_colors.append(colors)
        return cell_colors

    def draw_gantt(self):
        df = []
        start_timestamp = datetime.datetime.now().timestamp()
        # iterate through all jobs and their operations
        for job in range(self.job_size):
            j = 0
            while j < self.machine_size:
                # If the job does not have an operation on this machine, continue.
                if self.initial_process_time_channel[job, j] == 0:
                    j += 1
                    continue

                dict_op = dict()
                dict_op["Resource"] = f"Job {job + 1}"
                # calculate start and finish time of the operation
                finish_time = start_timestamp + self.last_schedule_finish_channel[job, j]
                start_time = finish_time - self.initial_process_time_channel[job, j] + self.job_machine_setup_time[job, self.job_machine_nos[job, j]]
                # return the date corresponding to the timestamp
                dict_op["Start"] = datetime.datetime.fromtimestamp(start_time)
                dict_op["Finish"] = datetime.datetime.fromtimestamp(finish_time)
                # retrieve the machine number corresponding to (job, operation j)
                dict_op["Task"] = f"Machine {self.job_machine_nos[job, j] + 1}"
                df.append(dict_op)

                dict_op = dict()
                dict_op["Resource"] = "Setup"
                finish_time = start_time
                start_time = finish_time - self.job_machine_setup_time[job, self.job_machine_nos[job, j]]
                # return the date corresponding to the timestamp
                dict_op["Start"] = datetime.datetime.fromtimestamp(start_time)
                dict_op["Finish"] = datetime.datetime.fromtimestamp(finish_time)
                # retrieve the machine number corresponding to (job, operation j)
                dict_op["Task"] = f"Machine {self.job_machine_nos[job, j] + 1}"
                df.append(dict_op)

                j += 1

        # sort the list of dictionary by job number and machine number
        df.sort(key=lambda k : int(k['Task'].split(' ')[1])) 
        # create additional colors since default colors of Plotly are limited to 10 different colors
        #r = lambda : np.random.randint(0,255)
        #colors = ['#%02X%02X%02X' % (r(), r(), r())]
        #for _ in range(1, len(df) + 1):
        #    colors.append('#%02X%02X%02X' % (r(), r(), r()))
        colors = px.colors.qualitative.Prism    

        fig = ff.create_gantt(df, colors=colors, index_col='Resource', show_colorbar=True, group_tasks=True, showgrid_x=True, title='Job Shop Scheduling')

        plot(fig, filename='RL_job_shop_scheduling.html')
