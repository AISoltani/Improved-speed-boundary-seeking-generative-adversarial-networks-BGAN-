#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import print_function

import sys
import os
import time

import numpy as np
import theano
import theano.tensor as T
from theano.sandbox.rng_mrg import MRG_RandomStreams as RandomStreams

import lasagne


# ################## Download and prepare the MNIST dataset ##################
# This is just some way of getting the MNIST dataset from an online location
# and loading it into numpy arrays. It doesn't involve Lasagne at all.

def load_dataset():
    # We first define a download function, supporting both Python 2 and 3.
    if sys.version_info[0] == 2:
        from urllib import urlretrieve
    else:
        from urllib.request import urlretrieve

    def download(filename, source='http://yann.lecun.com/exdb/mnist/'):
        print("Downloading %s" % filename)
        urlretrieve(source + filename, filename)

    # We then define functions for loading MNIST images and labels.
    # For convenience, they also download the requested files if needed.
    import gzip

    def load_mnist_images(filename):
        if not os.path.exists(filename):
            download(filename)
        # Read the inputs in Yann LeCun's binary format.
        with gzip.open(filename, 'rb') as f:
            data = np.frombuffer(f.read(), np.uint8, offset=16)
        # The inputs are vectors now, we reshape them to monochrome 2D images,
        # following the shape convention: (examples, channels, rows, columns)
        data = data.reshape(-1, 1, 28, 28)
        # The inputs come as bytes, we convert them to float32 in range [0,1].
        # (Actually to range [0, 255/256], for compatibility to the version
        # provided at http://deeplearning.net/data/mnist/mnist.pkl.gz.)
        return data / np.float32(256)

    def load_mnist_labels(filename):
        if not os.path.exists(filename):
            download(filename)
        # Read the labels in Yann LeCun's binary format.
        with gzip.open(filename, 'rb') as f:
            data = np.frombuffer(f.read(), np.uint8, offset=8)
        # The labels are vectors of integers now, that's exactly what we want.
        return data

    # We can now download and read the training and test set images and labels.
    X_train = load_mnist_images('train-images-idx3-ubyte.gz')
    y_train = load_mnist_labels('train-labels-idx1-ubyte.gz')
    X_test = load_mnist_images('t10k-images-idx3-ubyte.gz')
    y_test = load_mnist_labels('t10k-labels-idx1-ubyte.gz')

    # We reserve the last 10000 training examples for validation.
    X_train, X_val = X_train[:-10000], X_train[-10000:]
    y_train, y_val = y_train[:-10000], y_train[-10000:]

    # We just return all the arrays in order, as expected in main().
    # (It doesn't matter how we do this as long as we can read them again.)
    return X_train, y_train, X_val, y_val, X_test, y_test


# ##################### Build the neural network model #######################
# We create two models: The generator and the discriminator network. The
# generator needs a transposed convolution layer defined first.

class Deconv2DLayer(lasagne.layers.Layer):
    def __init__(self, incoming, num_filters, filter_size, stride=1, pad=0,
                 nonlinearity=lasagne.nonlinearities.rectify, **kwargs):
        super(Deconv2DLayer, self).__init__(incoming, **kwargs)
        self.num_filters = num_filters
        self.filter_size = lasagne.utils.as_tuple(filter_size, 2, int)
        self.stride = lasagne.utils.as_tuple(stride, 2, int)
        self.pad = lasagne.utils.as_tuple(pad, 2, int)
        self.W = self.add_param(lasagne.init.Orthogonal(),
                                (self.input_shape[1], num_filters) + self.filter_size,
                                name='W')
        self.b = self.add_param(lasagne.init.Constant(0),
                                (num_filters,),
                                name='b')
        if nonlinearity is None:
            nonlinearity = lasagne.nonlinearities.identity
        self.nonlinearity = nonlinearity

    def get_output_shape_for(self, input_shape):
        shape = tuple(i * s - 2 * p + f - 1
                      for i, s, p, f in zip(input_shape[2:],
                                            self.stride,
                                            self.pad,
                                            self.filter_size))
        return (input_shape[0], self.num_filters) + shape

    def get_output_for(self, input, **kwargs):
        op = T.nnet.abstract_conv.AbstractConv2d_gradInputs(
            imshp=self.output_shape,
            kshp=(self.input_shape[1], self.num_filters) + self.filter_size,
            subsample=self.stride, border_mode=self.pad)
        conved = op(self.W, input, self.output_shape[2:])
        if self.b is not None:
            conved += self.b.dimshuffle('x', 0, 'x', 'x')
        return self.nonlinearity(conved)
    
class GaussianSampleLayer(lasagne.layers.MergeLayer):
    def __init__(self, mu, logsigma, rng=None, **kwargs):
        self.rng = rng if rng else RandomStreams(lasagne.random.get_rng().randint(1,2147462579))
        super(GaussianSampleLayer, self).__init__([mu, logsigma], **kwargs)

    def get_output_shape_for(self, input_shapes):
        return input_shapes[0]

    def get_output_for(self, inputs, deterministic=False, **kwargs):
        mu, logsigma = inputs
        shape=(self.input_shapes[0][0] or inputs[0].shape[0],
                self.input_shapes[0][1] or inputs[0].shape[1])
        if deterministic:
            return mu
        return mu + T.exp(logsigma) * self.rng.normal(shape)

def build_generator(input_var=None):
    from lasagne.layers import InputLayer, ReshapeLayer, DenseLayer, batch_norm
    from lasagne.nonlinearities import sigmoid
    # input: 100dim
    layer = InputLayer(shape=(None, 100), input_var=input_var)
    # fully-connected layer
    layer = batch_norm(DenseLayer(layer, 1024))
    # project and reshape
    layer = batch_norm(DenseLayer(layer, 128 * 7 * 7))
    layer = ReshapeLayer(layer, ([0], 128, 7, 7))
    # two fractional-stride convolutions
    layer = batch_norm(Deconv2DLayer(layer, 64, 5, stride=2, pad=2))
    layer = Deconv2DLayer(layer, 1, 5, stride=2, pad=2,
                          nonlinearity=sigmoid)
    print("Generator output:", layer.output_shape)
    return layer

def build_discriminator(input_var=None):
    from lasagne.layers import (InputLayer, Conv2DLayer, ReshapeLayer,
                                DenseLayer, batch_norm)
    from lasagne.layers.dnn import Conv2DDNNLayer as Conv2DLayer  # override
    from lasagne.nonlinearities import LeakyRectify, sigmoid
    lrelu = LeakyRectify(0.2)
    # input: (None, 1, 28, 28)
    layer = InputLayer(shape=(None, 1, 28, 28), input_var=input_var)
    # two convolutions
    layer = Conv2DLayer(layer, 64, 5, stride=2, pad=2, nonlinearity=lrelu)
    layer = Conv2DLayer(layer, 128, 5, stride=2, pad=2, nonlinearity=lrelu)
    # fully-connected layer
    layer = DenseLayer(layer, 1024, nonlinearity=lrelu)
    # output layer
    mu = DenseLayer(layer, 100, nonlinearity=None)
    log_sigma = DenseLayer(layer, 100, nonlinearity=None)
    print("Discriminator output:", mu.output_shape)
    return mu, log_sigma

def build_decoder(input_var=None):
    from lasagne.layers import InputLayer, ReshapeLayer, DenseLayer, batch_norm
    from lasagne.nonlinearities import sigmoid
    
    layer = InputLayer(shape=(None, 100), input_var=input_var)
    # fully-connected layer
    layer = batch_norm(DenseLayer(layer, 1024))
    # project and reshape
    layer = batch_norm(DenseLayer(layer, 128 * 7 * 7))
    layer = ReshapeLayer(layer, ([0], 128, 7, 7))
    # two fractional-stride convolutions
    layer = batch_norm(Deconv2DLayer(layer, 64, 5, stride=2, pad=2))
    layer = Deconv2DLayer(layer, 1, 5, stride=2, pad=2, nonlinearity=sigmoid)
    print("Generator output:", layer.output_shape)
    return layer

# ############################# Batch iterator ###############################
# This is just a simple helper function iterating over training data in
# mini-batches of a particular size, optionally in random order. It assumes
# data is available as numpy arrays. For big datasets, you could load numpy
# arrays as memory-mapped files (np.load(..., mmap_mode='r')), or write your
# own custom data iteration function. For small datasets, you can also copy
# them to GPU at once for slightly improved performance. This would involve
# several changes in the main program, though, and is not demonstrated here.

def iterate_minibatches(inputs, targets, batchsize, shuffle=False):
    assert len(inputs) == len(targets)
    if shuffle:
        indices = np.arange(len(inputs))
        np.random.shuffle(indices)
    for start_idx in range(0, len(inputs) - batchsize + 1, batchsize):
        if shuffle:
            excerpt = indices[start_idx:start_idx + batchsize]
        else:
            excerpt = slice(start_idx, start_idx + batchsize)
        yield inputs[excerpt], targets[excerpt]

# ############################# BGAN Loss ###############################

def log_sum_exp(x, axis=None):
    '''Numerically stable log( sum( exp(A) ) ).
    '''
    x_max = T.max(x, axis=axis, keepdims=True)
    y = T.log(T.sum(T.exp(x - x_max), axis=axis, keepdims=True)) + x_max
    y = T.sum(y, axis=axis)
    return y

def norm_exp(log_factor):
    '''Gets normalized weights.
    '''
    log_factor = log_factor - T.log(log_factor.shape[0]).astype('float32')
    w_norm   = log_sum_exp(log_factor, axis=0)
    log_w    = log_factor - T.shape_padleft(w_norm)
    w_tilde  = T.exp(log_w)
    return w_tilde

def reweighted_loss(fake_out):
    log_d1 = -T.nnet.softplus(-fake_out)  # -D_cell.neg_log_prob(1., P=d)
    log_d0 = -fake_out - T.nnet.softplus(-fake_out)  # -D_cell.neg_log_prob(0., P=d)
    log_w = log_d1 - log_d0
    # Find normalized weights.
    log_N = T.log(log_w.shape[0]).astype('float32')
    log_Z_est = log_sum_exp(log_w - log_N, axis=0)
    log_Z_est = theano.gradient.disconnected_grad(log_Z_est)
    log_w_tilde = log_w - T.shape_padleft(log_Z_est) - log_N
    cost = ((log_w - T.maximum(log_Z_est, -2)) ** 2).mean()
    return cost

# ############################## Main program ################################
# Everything else will be handled in our main program now. We could pull out
# more functions to better separate the code, but it wouldn't make it any
# easier to read.

def kl_divergence(mu_1, mu_2, ls_1, ls_2, clip=-7):
    ls_1 = T.maximum(ls_2, clip)
    ls_2 = T.maximum(ls_2, clip)
    kl = ls_2 - ls_1 + 0.5 * ((T.exp(2 * ls_1) + (mu_2 - mu_1) ** 2) / T.exp(2 * ls_2) - 1)
    return kl.sum(axis=(kl.ndim-1)) 

def main(num_epochs=200, initial_eta=1e-5):
    # Load the dataset
    print("Loading data...")
    X_train, y_train, X_val, y_val, X_test, y_test = load_dataset()

    # Prepare Theano variables for inputs and targets
    noise_var = T.matrix('noise')
    input_var = T.tensor4('inputs')
    #    target_var = T.ivector('targets')

    # Create neural network model
    print("Building model and compiling functions...")
    generator = build_generator(noise_var)
    encoder = build_discriminator(input_var)
    dx = GaussianSampleLayer(encoder[0], encoder[1], name='d')
    decoder = build_decoder(lasagne.layers.get_output(dx))
    decoder_f = build_decoder(lasagne.layers.get_output(dx, lasagne.layers.get_output(generator)))

    # Create expression for passing real data through the discriminator
    mu_r, log_sigma_r = lasagne.layers.get_output(encoder)
    # Create expression for passing fake data through the discriminator
    mu_f, log_sigma_f = lasagne.layers.get_output(encoder, lasagne.layers.get_output(generator))
    
    mu_dr = lasagne.layers.get_output(decoder)
    mu_df = lasagne.layers.get_output(decoder_f)

    mu_pr = theano.shared(lasagne.utils.floatX(np.random.normal(size=(100,))), name='mu_pr')
    mu_pf = theano.shared(lasagne.utils.floatX(np.random.normal(size=(100,))), name='mu_pf')
    
    kl_r = kl_divergence(mu_r, mu_pr[None, :], log_sigma_r, 0.)
    kl_f = kl_divergence(mu_f, mu_pf[None, :], log_sigma_f, 0.)
    #kl_rf = kl_divergence(mu_pr, mu_f, 0., log_sigma_f) + kl_divergence(mu_pf, mu_r, 0., log_sigma_r)
    kl_rf = 0.5 * (kl_divergence(mu_pr, mu_pf, 0., 0.) + kl_divergence(mu_pf, mu_pr, 0., 0.))
    #kl_f2 = kl_divergence(mu_pr, mu_f, 0., log_sigma_f)
    kl_f2 = kl_divergence(mu_f, mu_pr, log_sigma_f, 0.)
    log_p_x_d_r = -T.nnet.binary_crossentropy(mu_dr, input_var)
    log_p_x_d_f = -T.nnet.binary_crossentropy(mu_df, theano.gradient.disconnected_grad(lasagne.layers.get_output(generator)))

    # Create loss expressions
    generator_loss = kl_f2.mean()
    #discriminator_loss = kl_r.mean() + kl_f.mean() - kl_rf.mean() - log_p_x_d_r.mean() - log_p_x_d_f.mean()
    discriminator_loss = kl_r.mean() + kl_f.mean() - log_p_x_d_r.mean() - log_p_x_d_f.mean()

    # Create update expressions for training
    generator_params = lasagne.layers.get_all_params(generator, trainable=True)
    discriminator_params = lasagne.layers.get_all_params(encoder, trainable=True)
    discriminator_params = lasagne.layers.get_all_params(decoder, trainable=True)
    discriminator_params += [mu_pr, mu_pf]
    eta = theano.shared(lasagne.utils.floatX(initial_eta))
    updates = lasagne.updates.adam(
        generator_loss, generator_params, learning_rate=eta, beta1=0.5)
    updates.update(lasagne.updates.adam(
        discriminator_loss, discriminator_params, learning_rate=eta, beta1=0.5))

    # Compile a function performing a training step on a mini-batch (by giving
    # the updates dictionary) and returning the corresponding training loss:
    train_fn = theano.function([noise_var, input_var],
                               [kl_rf.mean(), kl_f2.mean()],
                               updates=updates)

    # Compile another function generating some data
    gen_fn = theano.function([noise_var],
                             lasagne.layers.get_output(generator,
                                                       deterministic=True))

    # Finally, launch the training loop.
    print("Starting training...")
    # We iterate over epochs:
    for epoch in range(num_epochs):
        # In each epoch, we do a full pass over the training data:
        train_err = 0
        train_batches = 0
        start_time = time.time()
        for batch in iterate_minibatches(X_train, y_train, 64, shuffle=True):
            inputs, targets = batch
            noise = lasagne.utils.floatX(np.random.rand(len(inputs), 100))
            train_err += np.array(train_fn(noise, inputs))
            train_batches += 1

        # Then we print the results for this epoch:
        print("Epoch {} of {} took {:.3f}s".format(
            epoch + 1, num_epochs, time.time() - start_time))
        print("  training loss:\t\t{}".format(train_err / train_batches))

        # And finally, we plot some generated data
        samples = gen_fn(lasagne.utils.floatX(np.random.rand(100, 100)))
        try:
            import matplotlib.pyplot as plt
        except ImportError:
            pass
        else:
            plt.imsave('/home/devon/Outs/mnist_bgan_2/{}.png'.format(epoch),
                       (samples.reshape(10, 10, 28, 28)
                        .transpose(0, 2, 1, 3)
                        .reshape(10 * 28, 10 * 28)),
                       cmap='gray')

        # After half the epochs, we start decaying the learn rate towards zero
        if epoch >= num_epochs // 2:
            progress = float(epoch) / num_epochs
            eta.set_value(lasagne.utils.floatX(initial_eta * 2 * (1 - progress)))

    # Optionally, you could now dump the network weights to a file like this:
    #np.savez('mnist_gen.npz', *lasagne.layers.get_all_param_values(generator))
    #np.savez('mnist_disc.npz', *lasagne.layers.get_all_param_values(discriminator))
    #
    # And load them again later on like this:
    # with np.load('model.npz') as f:
    #     param_values = [f['arr_%d' % i] for i in range(len(f.files))]
    # lasagne.layers.set_all_param_values(network, param_values)


if __name__ == '__main__':
    if ('--help' in sys.argv) or ('-h' in sys.argv):
        print("Trains a DCGAN on MNIST using Lasagne.")
        print("Usage: %s [EPOCHS]" % sys.argv[0])
        print()
        print("EPOCHS: number of training epochs to perform (default: 100)")
    else:
        kwargs = {}
        if len(sys.argv) > 1:
            kwargs['num_epochs'] = int(sys.argv[1])
        main(**kwargs)