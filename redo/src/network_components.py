#!/usr/bin/python3

"""network_components.py - Implementation of the components of the various
                           networks

For the NeurIPS Reproducibility Challenge and the DD2412 Deep Learning, Advanced
course at KTH Royal Institute of Technology.
"""
__author__ = "Adrian Chmielewski-Anders, Mats Steinweg & Bas Straathof"


import tensorflow as tf
from tensorflow.keras.layers import Layer, Dense, LayerNormalization, ReLU, \
        Conv2D, MaxPool2D, Softmax, AveragePooling2D, MaxPool2D
from tensorflow.keras.initializers import orthogonal
from typing import Tuple, Union


class SpectralNormalization(Layer):
    """Spectral normalization layer to wrap around a Conv2D Layer. Kernel
    weights are normalized before each forward pass."""
    def __init__(self, layer: Conv2D, n_power_iterations: int = 1):
        """Class constructor

        Attributes:
            layer: Conv2D object
            n_power_iterations: Number of power iterations
        """
        super(SpectralNormalization, self).__init__()

        # Conv2D containing weights to be normalized
        self.layer = layer

        # Number of power iterations (spectral norm approximation)
        self.n_power_iterations = n_power_iterations

        # Conv2D layer's weights haven't been initialized yet
        # Will be initialized on first forward pass
        self.init = False

        # Initialize u (approximated singular vector)
        # Non-trainable variable will be updated every iteration
        self.u = super().add_weight(name='u', shape=[self.layer.filters, 1],
            initializer=tf.random_normal_initializer, trainable=False)

    def call(self, x: tf.Tensor, training: bool) -> tf.Tensor:
        """Perform forward pass of Conv2D layer on first iteration to initialize
        the weights

        Args:
            x: The input
            training: Whether we are training
        """
        # Perform forward pass of Conv2D layer on first iteration to initialize
        # the weights. Introduce 'kernel_orig' as trainable variables
        if not self.init:
            # Initialize Conv2D layer
            _ = self.layer(x)

            self.layer.kernel_orig = self.layer.add_weight('kernel_orig',
                    self.layer.kernel.shape, trainable=True)

            # Get layer's weights. Contains 'kernel', 'kernel_orig' and
            # possibly 'bias'
            weights = self.layer.get_weights()

            # Set 'kernel_orig' to network's weights. 'kernel_orig' will be
            # updated, 'kernel' will be normalized and used in the forward pass
            if len(weights) == 2:
                # Conv layer without bias
                self.layer.set_weights([weights[0], weights[0]])
            else:
                # Conv layer with bias
                self.layer.set_weights([weights[0], weights[1], weights[0]])

            # SN layer initialized
            self.init = True

        # Normalize weights before every forward pass
        W_sn = self.normalize_weights(training=training)

        # Assign normalized weights to kernel for forward pass
        # Weight's are assigned as tf.Tensor in order not to
        # add a new variable to the model
        self.layer.kernel = W_sn

        # perform forward pass of Conv2d layer
        output = self.layer(x)

        return output


    def normalize_weights(self, training: bool):
        """Normalize the Conv2D layer's weights w.r.t. their spectral norm

        Args:
            training: whether we are training

        Returns:
            W_sn; Spectrally normalized weights
        """

        # Number of filter kernels in Conv2D layer
        filters = self.layer.weights[0].shape.as_list()[-1]

        # Get original weights
        W_orig = self.layer.kernel_orig

        # Reshape kernel weights
        W_res = tf.reshape(W_orig, [filters, -1])

        # Compute spectral norm and singular value approximation
        spectral_norm, u = self.power_iteration(W_res)

        # Normalize kernel weights
        W_sn = W_orig / spectral_norm

        if training:
            # Update estimate of singular vector during training
            self.u.assign(u)

        return W_sn


    def power_iteration(self, W: tf.Tensor) -> Tuple[tf.Tensor, tf.Tensor]:
        """Compute approximate spectral norm.

        Note: According to the paper n_power_iterations= 1 is sufficient due
              to updated u.

        Args:
            W: Reshaped kernel weights | shape: [filters, N]

        Returns:
            Approximate spectral norm and updated singular vector
            approximation.
        """
        for _ in range(self.n_power_iterations):
            v = self.normalize_l2(tf.matmul(W, self.u, transpose_a=True))
            u = self.normalize_l2(tf.matmul(W, v))
            spectral_norm = tf.matmul(tf.matmul(u, W, transpose_a=True), v)

        return spectral_norm, u


    @staticmethod
    def normalize_l2(v: tf.Tensor, epsilon: float = 1e-12) -> tf.Tensor:
        """Normalize input matrix w.r.t. its euclidean norm

        Args:
            v: Input matrix
            epsilon: Small epsilon to avoid division by zero

        Returns:
            l2-normalized input matrix
        """
        return v / (tf.math.reduce_sum(v**2)**0.5 + epsilon)


class SelfAttentionModule(Layer):
    """Self-attention component for GANs"""
    def __init__(self, init_gain: float, output_channels: int,
            key_size: int = None):
        """Class constructor

        Attributes:
            init_gain: Initializer gain for orthogonal initialization
            output_channels: Number of output channels
            key_size: The number of key channels
        """
        super(SelfAttentionModule, self).__init__()

        # Set number of key channels
        if key_size is None:
            self.key_size = output_channels // 8
        else:
            self.key_size = key_size

        # Trainable parameter to control influence of learned attention maps
        # Initialize to 0, so network can learn to use attention maps
        self.gamma = self.add_weight(name='self_attention_gamma',
                initializer=tf.zeros_initializer())

        # map pooling to reduce memory print
        self.max_pool = MaxPool2D(pool_size=(2, 2))

        # Learned transformation
        self.f = SpectralNormalization(Conv2D(
            filters=self.key_size, kernel_size=(1, 1),
            kernel_initializer=orthogonal(gain=init_gain), use_bias=False))
        self.g = SpectralNormalization(Conv2D(filters=self.key_size,
            kernel_size=(1, 1), kernel_initializer=orthogonal(gain=init_gain),
            use_bias=False))
        self.h = SpectralNormalization(Conv2D(filters=output_channels//2,
            kernel_size=(1, 1), kernel_initializer=orthogonal(gain=init_gain),
            use_bias=False))
        self.out = SpectralNormalization(Conv2D(filters=output_channels,
            kernel_size=(1, 1), kernel_initializer=orthogonal(gain=init_gain),
            use_bias=False))


    def call(self, x: tf.Tensor, training: bool) -> tf.Tensor:
        """Perform call of attention layer
        Args:
            x: Input to the residual block
            training: Whether we are training
        """
        o = self.compute_attention(x, training)
        y = self.gamma * o + x

        return y


    def compute_attention(self, x: tf.Tensor, training: bool) -> tf.Tensor:
        """Compute attention maps

        Args:
            x: Input to the residual block
            training: Whether we are training

        Returns:
            Attention map with same shape as input feature maps
        """
        # Height, width, channel
        h, w, c = x.shape.as_list()[1:]

        # Compute and reshape features
        fx = tf.reshape(self.f.call(x, training), [-1, h * w, self.key_size])
        gx = self.g.call(x, training)

        # Down-sample features to reduce memory print
        gx = self.max_pool(gx)
        gx = tf.reshape(gx, [-1, (h * w)//4, self.key_size])
        s = tf.matmul(fx, gx, transpose_b=True)

        beta = Softmax(axis=2)(s)

        hx = self.h.call(x, training)
        hx = self.max_pool(hx)
        hx = tf.reshape(hx, [-1, (h * w)//4, c//2])

        interim = tf.matmul(beta, hx)
        interim = tf.reshape(interim, [-1, h, w, c//2])
        o = self.out.call(interim, training)

        return o


class ResidualBlock(Layer):
    """Residual computation block with down-sampling option and variable number
    of output channels."""
    def __init__(self, init_gain: float, stride: Union[int, Tuple[int, int]],
            output_channels: int, first_block=False, last_block=False):
        """Class constructor

        Args:
            init_gain: Initializer gain for orthogonal initialization
            stride: Stride of the convolutional layers
            output_channels: Number of output channels
            first_block: True if first residual block of network
            last_block: True if last residual block of network
        """
        super(ResidualBlock, self).__init__()

        # True if first residual block in the network
        self.first_block = first_block

        # True if last residual block in the network
        self.last_block = last_block

        # Perform 1x1 convolutions on the identity to adjust the number of
        # channels to the output of the residual pipeline
        self.process_identity = SpectralNormalization(Conv2D(
            filters=output_channels, kernel_size=(1, 1), strides=stride,
            kernel_initializer=orthogonal(gain=init_gain)))

        # Residual pipeline
        self.conv_1 = SpectralNormalization(Conv2D(
            filters=output_channels, kernel_size=(3, 3), strides=stride,
            padding='same', kernel_initializer=orthogonal(gain=init_gain)))
        self.relu = ReLU()
        self.conv_2 = SpectralNormalization(Conv2D(filters=output_channels,
            kernel_size=(3, 3), padding='same',
            kernel_initializer=orthogonal(gain=init_gain)))

        # only create pooling layer if down-sampling block
        self.pool = AveragePooling2D(pool_size=(2, 2))


    def call(self, x: tf.Tensor, training: bool) -> tf.Tensor:
        """Perform call of residual block layer call

        Args:
            x: Input to the residual block
            training: Whether we are training
        """

        # Perform ReLU if not first block
        if not self.first_block: h = self.relu(x)
        else: h = x

        # Pass input through pipeline
        h = self.conv_1.call(h, training)
        h = self.relu(h)
        h = self.conv_2.call(h, training)

        # Down-sample residual features
        if not self.last_block:
            h = self.pool(h)

        # Process identity
        if h.shape != x.shape:
            # Down-sample identity to match residual dimensions
            if not self.last_block:
                x = self.pool(x)
            x = self.process_identity.call(x, training)

        # Skip-connection
        x += h

        return x


class InstanceNormalization(Layer):
    def __init__(self, filters, affine=False):
        """Class constructor

        Attributs:
            affine: Whether the function is affine
        """
        super(InstanceNormalization, self).__init__()
        self.affine = affine
        self.eps = 1e-12
        if self.affine:
            self.gamma = self.add_weight(name='gamma', shape=[1, filters],
                                        initializer=tf.ones_initializer, trainable=True)
            self.beta = self.add_weight(name='beta', shape=[1, filters],
                                        initializer=tf.zeros_initializer, trainable=True)


    def call(self, x: tf.Tensor) -> tf.Tensor:
        """Perform instance normalization

        Args:
            x: Input tensor

        Returns:
            x: Instance normalized tensor
        """

        # Compute mean over spatial dimensions H, W
        mean = tf.expand_dims(tf.math.reduce_mean(x, axis=(1, 2)), axis=1)
        # Reshape mean to match shape of x
        mean = tf.expand_dims(mean, axis=2)

        # Compute std over spatial dimensions H, W
        std = tf.expand_dims(tf.math.reduce_std(x, axis=(1, 2)), axis=1)
        # Reshape std to match shape of x
        std = tf.expand_dims(std, axis=2)

        # Normalize x
        x = (x - mean) / (std + self.eps)

        # Scale and shift x if affine is true
        if self.affine:
            x = x * self.gamma + self.beta

        return x

