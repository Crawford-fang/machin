import numpy as np
import torch
import torch.nn as nn

from .base import TorchFramework
from .utils import safe_call
from .replay_buffer import ReplayBuffer, Transition
from ..models.base import NeuralNetworkModule
from typing import Union, Dict

from ..noise.action_space_noise import *

from utils.visualize import visualize_graph


class PPO(TorchFramework):
    def __init__(self,
                 actor: Union[NeuralNetworkModule, nn.Module],
                 critic: Union[NeuralNetworkModule, nn.Module],
                 optimizer,
                 criterion,
                 entropy_weight=None,
                 surrogate_loss_clip=0.2,
                 gradient_max=np.inf,
                 learning_rate=0.001,
                 lr_scheduler=None,
                 lr_scheduler_params=None,
                 batch_size=100,
                 update_times=50,
                 discount=0.99,
                 replay_size=5000,
                 replay_device="cpu",
                 reward_func=None):
        """
        Initialize PPO framework.
        Note: when given a state, (and an optional action) actor must at least return two
        values:
        1. Action
            For contiguous environments, action must be of shape [batch_size, action_dim]
            and clamped to environment limits.
            For discreet environments, action must be of shape [batch_size, action_dim],
            it could be a categorical encoded integer, or a one hot vector.

            Actions are given by samples during training in PPO framework. When actor is
            given a batch of actions and states, it must evaluate the states, and return
            the log likelihood of the given actions instead of re-sampling actions.

        2. Log likelihood of action (action probability)
            For contiguous environments, action's are not directly output by your actor,
            otherwise it would be rather inconvenient to generate this value, instead, your
            actor network should output parameters for a certain distribution (eg: normal)
            and then be drawn from it.

            For discreet environments, action probability is the one of the values in your
            one-hot vector. It is recommended to sample from torch.distribution.Categorical,
            instead of sampling by yourself.

            Action probability must be differentiable, actor will receive its gradient from
            the gradient of action probability.

        The third entropy value is optional:
        3. Entropy of action distribution (Optional)
            Entropy is usually calculated using dist.entropy(), it will be considered if you
            have specified the entropy_weight argument.

            An example of your actor in contiguous environments::

                class ActorNet(nn.Module):
                    def __init__(self):
                        super(ActorNet, self).__init__()
                        self.fc = nn.Linear(3, 100)
                        self.mu_head = nn.Linear(100, 1)
                        self.sigma_head = nn.Linear(100, 1)

                    def forward(self, state, action=None):
                        x = t.relu(self.fc(state))
                        mu = 2.0 * t.tanh(self.mu_head(x))
                        sigma = F.softplus(self.sigma_head(x))
                        dist = Normal(mu, sigma)
                        action = action if action is not None else dist.sample()
                        action_log_prob = dist.log_prob(action)
                        action_entropy = dist.entropy()
                        action = action.clamp(-2.0, 2.0)
                        return action.detach(), action_log_prob, action_entropy

        """
        self.batch_size = batch_size
        self.update_times = update_times
        self.discount = discount
        self.rpb = ReplayBuffer(replay_size, replay_device)

        self.entropy_weight = entropy_weight
        self.surr_clip = surrogate_loss_clip
        self.grad_max = gradient_max

        self.actor = actor
        self.critic = critic
        self.actor_optim = optimizer(self.actor.parameters(), learning_rate)
        self.critic_optim = optimizer(self.critic.parameters(), learning_rate)

        if lr_scheduler is not None:
            self.actor_lr_sch = lr_scheduler(self.actor_optim, *lr_scheduler_params[0])
            self.critic_lr_sch = lr_scheduler(self.critic_optim, *lr_scheduler_params[1])

        self.criterion = criterion

        self.reward_func = PPO.bellman_function if reward_func is None else reward_func

        super(PPO, self).__init__()
        self.set_top(["actor", "critic"])
        self.set_restorable(["actor", "critic"])

    def act(self, state):
        """
        Use actor network to give a policy to the current state.

        Returns:
            Anything produced by actor.
        """
        return safe_call(self.actor, state)

    def eval_act(self, state, action):
        """
        Use actor network to evaluate the log-likelihood of a given action in the current state.

        Returns:
            Anything produced by actor.
        """
        return safe_call(self.actor, state, action)

    def criticize(self, state):
        """
        Use critic network to evaluate current value.

        Returns:
            Value evaluated by critic.
        """
        return safe_call(self.critic, state)

    def store_observe(self, transition: Union[Transition, Dict]):
        """
        Add a transition sample to the replay buffer. Transition samples will be used in update()
        observe() is used during training.
        """
        self.rpb.append(transition)

    def set_reward_func(self, rf):
        """
        Set reward function, default reward function is bellman function with no extra inputs
        """
        self.reward_func = rf

    def get_replay_buffer(self):
        return self.rpb

    def update(self, update_value=True, update_policy=True,
               concatenate_samples=True, next_value_use_rollout=True):
        """
        Args:
            next_value_use_rollout: use rollout values as next values instead of using critic net to
                                    estimate the next value
        """
        sum_act_policy_loss = 0
        sum_value_loss = 0

        if next_value_use_rollout:
            batch_size, (state, action, reward, next_state, terminal,
                         action_log_prob, target_value, *others) = \
                self.rpb.sample_batch(self.batch_size,
                                      sample_method="all",
                                      concatenate=concatenate_samples,
                                      sample_keys=["state", "action", "reward", "next_state",
                                                   "terminal", "action_log_prob", "value", "*"],
                                      additional_concat_keys=["action_log_prob", "value"])
        else:
            batch_size, (state, action, reward, next_state, terminal,
                         action_log_prob, *others) = \
                self.rpb.sample_batch(self.batch_size,
                                      sample_method="all",
                                      concatenate=concatenate_samples,
                                      sample_keys=["state", "action", "reward", "next_state",
                                                   "terminal", "action_log_prob", "*"],
                                      additional_concat_keys=["action_log_prob"])
            next_value = self.criticize(next_state)
            target_value = self.reward_func(reward, self.discount, next_value, terminal, *others).detach()

        # normalize target value
        target_value = (target_value - target_value.mean()) / (target_value.std() + 1e-5)

        for i in range(self.update_times):
            value = self.criticize(state)
            with torch.no_grad():
                advantage = target_value.to(value.device) - value

            if self.entropy_weight is not None:
                new_action, new_action_log_prob, new_action_entropy = self.eval_act(state, action)

            else:
                new_action, new_action_log_prob, *_ = self.eval_act(state, action)

            new_action_log_prob = new_action_log_prob.view(batch_size, 1)

            sim_ratio = t.exp(new_action_log_prob -
                              action_log_prob.to(new_action_log_prob.device).detach())
            surr_loss_1 = sim_ratio * advantage
            surr_loss_2 = t.clamp(sim_ratio, 1 - self.surr_clip, 1 + self.surr_clip) * advantage

            act_policy_loss = -t.min(surr_loss_1, surr_loss_2)

            if self.entropy_weight is not None:
                act_policy_loss += self.entropy_weight * new_action_entropy.mean()
            act_policy_loss = act_policy_loss.mean()

            value_loss = self.criterion(target_value.to(value.device), value)

            # Update actor network
            self.actor.zero_grad()
            act_policy_loss.backward()
            nn.utils.clip_grad_norm_(self.actor.parameters(), self.grad_max)
            self.actor_optim.step()
            sum_act_policy_loss += act_policy_loss.item()

            # Update critic network
            self.critic.zero_grad()
            value_loss.backward()
            nn.utils.clip_grad_norm_(self.critic.parameters(), self.grad_max)
            self.critic_optim.step()
            sum_value_loss += value_loss.item()

        self.rpb.clear()
        return -sum_act_policy_loss, sum_value_loss

    def update_lr_scheduler(self):
        if hasattr(self, "actor_lr_sch"):
            self.actor_lr_sch.step()
        if hasattr(self, "critic_lr_sch"):
            self.critic_lr_sch.step()

    @staticmethod
    def bellman_function(reward, discount, next_value, terminal, *_):
        next_value = next_value.to(reward.device)
        terminal = terminal.to(reward.device)
        return reward + discount * (1 - terminal) * next_value
