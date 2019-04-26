from __future__ import absolute_import, print_function

from niftynet.application.base_application import BaseApplication
from niftynet.engine.sampler_uniform_v2 import UniformSampler

import tensorflow as tf

from niftynet.engine.application_factory import ApplicationNetFactory, InitializerFactory, OptimiserFactory
from niftynet.engine.windows_aggregator_classifier import ClassifierSamplesAggregator
from niftynet.io.image_reader import ImageReader
from niftynet.layer.binary_masking import BinaryMaskingLayer
from niftynet.layer.histogram_normalisation import HistogramNormalisationLayer
from niftynet.layer.mean_variance_normalisation import MeanVarNormalisationLayer
from niftynet.layer.rand_flip import RandomFlipLayer
from niftynet.layer.rand_rotation import RandomRotationLayer
from niftynet.layer.rand_spatial_scaling import RandomSpatialScalingLayer
from niftynet.layer.loss_segmentation import LossFunction
from niftynet.engine.application_variables import CONSOLE, NETWORK_OUTPUT, TF_SUMMARIES
from niftynet.layer.post_processing import PostProcessingLayer

class LandmarkApplication(BaseApplication):
    REQUIRED_CONFIG_SECTION = "LANDMARK"

    def __init__(self, net_param, action_param, action):
        super(LandmarkApplication, self).__init__()
        tf.logging.info('starting regression application')
        self.action = action

        self.net_param = net_param
        self.action_param = action_param

        self.data_param = None
        self.landmark_param = None
        self.SUPPORTED_SAMPLING = {
            'uniform': self.initialise_uniform_sampler
        }
        # TODO: Add more sampling methods


    def initialise_dataset_loader(
            self, data_param=None, task_param=None, data_partitioner=None):
        """
        this function initialise self.readers

        :param data_param: input modality specifications
        :param task_param: contains task keywords for grouping data_param
        :param data_partitioner:
                           specifies train/valid/infer splitting if needed
        :return:
        """

        self.data_param = data_param
        self.landmark_param = task_param

        if self.is_training:
            reader_names = ('image', 'label', 'sampler')
        elif self.is_inference:
            reader_names = ('image',)
        elif self.is_evaluation:
            reader_names = ('image', 'label', 'inferred')
        else:
            tf.logging.fatal(
                'Action `%s` not supported. Expected one of %s',
                self.action, self.SUPPORTED_PHASES)
            raise ValueError

        try:
            reader_phase = self.action_param.dataset_to_infer
        except AttributeError:
            reader_phase = None
        file_lists = data_partitioner.get_file_lists_by(
            phase=reader_phase, action=self.action)
        self.readers = [
            ImageReader(reader_names).initialise(
                data_param, task_param, file_list) for file_list in file_lists]

        # initialise input preprocessing layers
        foreground_masking_layer = BinaryMaskingLayer(
            type_str=self.net_param.foreground_type,
            multimod_fusion=self.net_param.multimod_foreground_type,
            threshold=0.0) \
            if self.net_param.normalise_foreground_only else None
        mean_var_normaliser = MeanVarNormalisationLayer(
            image_name='image', binary_masking_func=foreground_masking_layer) \
            if self.net_param.whitening else None
        histogram_normaliser = HistogramNormalisationLayer(
            image_name='image',
            modalities=vars(task_param).get('image'),
            model_filename=self.net_param.histogram_ref_file,
            binary_masking_func=foreground_masking_layer,
            norm_type=self.net_param.norm_type,
            cutoff=self.net_param.cutoff,
            name='hist_norm_layer') \
            if (self.net_param.histogram_ref_file and
                self.net_param.normalisation) else None

        normalisation_layers = []
        if histogram_normaliser is not None:
            normalisation_layers.append(histogram_normaliser)
        if mean_var_normaliser is not None:
            normalisation_layers.append(mean_var_normaliser)

        augmentation_layers = []
        if self.is_training:
            train_param = self.action_param
            if train_param.random_flipping_axes != -1:
                augmentation_layers.append(RandomFlipLayer(
                    flip_axes=train_param.random_flipping_axes))
            if train_param.scaling_percentage:
                augmentation_layers.append(RandomSpatialScalingLayer(
                    min_percentage=train_param.scaling_percentage[0],
                    max_percentage=train_param.scaling_percentage[1],
                    antialiasing=train_param.antialiasing,
                    isotropic=train_param.isotropic_scaling))
            if train_param.rotation_angle or \
                    self.action_param.rotation_angle_x or \
                    self.action_param.rotation_angle_y or \
                    self.action_param.rotation_angle_z:
                rotation_layer = RandomRotationLayer()
                if train_param.rotation_angle:
                    rotation_layer.init_uniform_angle(
                        train_param.rotation_angle)
                else:
                    rotation_layer.init_non_uniform_angle(
                        self.action_param.rotation_angle_x,
                        self.action_param.rotation_angle_y,
                        self.action_param.rotation_angle_z)
                augmentation_layers.append(rotation_layer)


        # only add augmentation to first reader (not validation reader)
        self.readers[0].add_preprocessing_layers(
             normalisation_layers + augmentation_layers)

        for reader in self.readers[1:]:
            reader.add_preprocessing_layers(normalisation_layers)

        # TODO: first attempt at initialise_dataset_loader

    def initialise_sampler(self):
        """
        Samplers take ``self.reader`` as input and generates
        sequences of ImageWindow that will be fed to the networks

        This function sets ``self.sampler``.
        """
        if self.is_training:
            self.SUPPORTED_SAMPLING[self.net_param.window_sampling][0]()
        else:
            self.SUPPORTED_SAMPLING[self.net_param.window_sampling][1]()
        # TODO: first attempt at initialise_sampler

    def initialise_network(self):
        """
        This function create an instance of network and sets ``self.net``

        :return: None
        """
        # TODO: check that this is the way we ought to initialize the network
        #  ( this is how segmentation_application.py and classification_application.py initialize the network )
        # raise NotImplementedError
        w_regularizer = None
        b_regularizer = None
        reg_type = self.net_param.reg_type.lower()
        decay = self.net_param.decay
        if reg_type == 'l2' and decay > 0:
            from tensorflow.contrib.layers.python.layers import regularizers
            w_regularizer = regularizers.l2_regularizer(decay)
            b_regularizer = regularizers.l2_regularizer(decay)
        elif reg_type == 'l1' and decay > 0:
            from tensorflow.contrib.layers.python.layers import regularizers
            w_regularizer = regularizers.l1_regularizer(decay)
            b_regularizer = regularizers.l1_regularizer(decay)

        self.net = ApplicationNetFactory.create(self.net_param.name)(
            num_classes=self.landmark_param.num_classes,
            w_initializer=InitializerFactory.get_initializer(
                name=self.net_param.weight_initializer),
            b_initializer=InitializerFactory.get_initializer(
                name=self.net_param.bias_initializer),
            w_regularizer=w_regularizer,
            b_regularizer=b_regularizer,
            acti_func=self.net_param.activation_function)

    def initialise_uniform_sampler(self):
        self.sampler = [[UniformSampler(
            reader=reader,
            window_sizes=self.data_param,
            batch_size=self.net_param.batch_size,
            windows_per_image=self.action_param.sample_per_volume,
            queue_length=self.net_param.queue_length) for reader in
            self.readers]]

    def initialise_aggregator(self):
        # TODO: check that ClassifierSamplesAggregator is the output_decoder we want
        #  ( this is how classification_application.py initializes the network )
        self.output_decoder = ClassifierSamplesAggregator(
            image_reader=self.readers[0],
            output_path=self.action_param.save_seg_dir,
            postfix=self.action_param.output_postfix)

    def connect_data_and_network(self,
                                 outputs_collector=None,
                                 gradients_collector=None):
        """
        Adding sampler output tensor and network tensors to the graph.

        :param outputs_collector:
        :param gradients_collector:
        :return:
        """

        def switch_sampler(for_training):
            with tf.name_scope('train' if for_training else 'validation'):
                sampler = self.get_sampler()[0][0 if for_training else -1]
                return sampler.pop_batch_op()

        if self.is_training:
            if self.action_param.validation_every_n > 0:
                data_dict = tf.cond(tf.logical_not(self.is_validation),
                                    lambda: switch_sampler(for_training=True),
                                    lambda: switch_sampler(for_training=False))
            else:
                data_dict = switch_sampler(for_training=True)


            image = tf.cast(data_dict['image'], tf.float32)
            net_args = {'is_training': self.is_training,
                        'keep_prob': self.net_param.keep_prob}
            net_out = self.net(image, **net_args)

            with tf.name_scope('Optimiser'):
                optimiser_class = OptimiserFactory.create(
                    name=self.action_param.optimiser)
                self.optimiser = optimiser_class.get_instance(
                    learning_rate=self.action_param.lr)

            loss_func = LossFunction(
                n_class=self.classification_param.num_classes,
                loss_type=self.action_param.loss_type)
            data_loss = loss_func(
                prediction=net_out,
                ground_truth=data_dict.get('label', None))
            reg_losses = tf.get_collection(tf.GraphKeys.REGULARIZATION_LOSSES)
            if self.net_param.decay > 0.0 and reg_losses:
                reg_loss = tf.reduce_mean(
                    [tf.reduce_mean(reg_loss) for reg_loss in reg_losses])
                loss = data_loss + reg_loss
            else:
                loss = data_loss
            grads = self.optimiser.compute_gradients(
                loss, colocate_gradients_with_ops=True)
            # collecting gradients variables
            gradients_collector.add_to_collection([grads])
            # collecting output variables
            outputs_collector.add_to_collection(
                var=data_loss, name='data_loss',
                average_over_devices=False, collection=CONSOLE)
            outputs_collector.add_to_collection(
                var=data_loss, name='data_loss',
                average_over_devices=True, summary_type='scalar',
                collection=TF_SUMMARIES)
            #TODO: Decide if we need to call self.add_confusion_matrix_summaries_
        else:
            data_dict = switch_sampler(for_training=False)
            image = tf.cast(data_dict['image'], tf.float32)

            net_args = {'is_training': self.is_training,
                        'keep_prob': self.net_param.keep_prob}
            net_out = self.net(image, **net_args)
            tf.logging.info(
                'net_out.shape may need to be resized: %s', net_out.shape)
            output_prob = self.landmark_param.output_prob
            num_classes = self.landmark_param.num_classes
            post_process_layer = PostProcessingLayer('IDENTITY', num_classes=num_classes)
            net_out = post_process_layer(net_out)

            outputs_collector.add_to_collection(
                var=net_out, name='window',
                average_over_devices=False, collection=NETWORK_OUTPUT)
            outputs_collector.add_to_collection(
                var=data_dict['image_location'], name='location',
                average_over_devices=False, collection=NETWORK_OUTPUT)
            self.initialise_aggregator()

        # TODO: connect_data_and_network (done by initialise_aggregator())
        # raise NotImplementedError

    def interpret_output(self, batch_output):
        """
        Implement output interpretations, e.g., save to hard drive
        cache output windows.

        :param batch_output: outputs by running the tf graph
        :return: True indicates the driver should continue the loop
            False indicates the drive should stop
        """
        # TODO: Check that interpret_output is implemented correctly
        #  (this is how classification_application implements it)
        if not self.is_training:
            n_samples = batch_output['window'].shape[0]



            # return self.output_decoder.decode_batch(
            #     batch_output['window'], batch_output['location'])
        return True

    # def initialise_evaluator(self, eval_param):
    #     self.eval_param = eval_param
    #     # TODO: set self.evaluator
    #
    #     raise NotImplementedError
