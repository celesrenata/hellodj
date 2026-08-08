"""Convert openWakeWord ONNX model to TFLite by reconstructing the DNN from extracted weights.
Automatically handles any layer_size and n_blocks by reading the ONNX graph."""
import sys
import os
import argparse
import re
import numpy as np
import onnx

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'


def convert(onnx_path, tflite_path):
    import tensorflow as tf

    # Load ONNX model
    onnx_model = onnx.load(onnx_path)
    graph = onnx_model.graph

    # Extract weights
    weights = {}
    for init in graph.initializer:
        weights[init.name] = onnx.numpy_helper.to_array(init)

    # Parse architecture from ONNX initializers
    layer1_w = weights['layer1.weight']  # [layer_size, 1536]
    layer_size = layer1_w.shape[0]

    # Count blocks by scanning for 'blocks.*.fcn_layer.weight'
    n_blocks = 0
    for name in weights:
        m = re.match(r'blocks\.(\d+)\.fcn_layer\.weight', name)
        if m:
            n_blocks = max(n_blocks, int(m.group(1)) + 1)

    # Build Keras model
    inp = tf.keras.layers.Input(shape=(16, 96), name='input')
    x = tf.keras.layers.Flatten()(inp)

    # Layer 1
    d1 = tf.keras.layers.Dense(layer_size, use_bias=True, name='layer1')
    x = d1(x)
    ln1 = tf.keras.layers.LayerNormalization(epsilon=1e-5, name='layernorm1')
    x = ln1(x)
    x = tf.keras.layers.ReLU()(x)

    # Blocks
    dense_layers = [d1]
    norm_layers = [ln1]
    for i in range(n_blocks):
        d = tf.keras.layers.Dense(layer_size, use_bias=True, name=f'blocks.{i}.fcn_layer')
        dense_layers.append(d)
        x = d(x)
        ln = tf.keras.layers.LayerNormalization(epsilon=1e-5, name=f'blocks.{i}.layer_norm')
        norm_layers.append(ln)
        x = ln(x)
        x = tf.keras.layers.ReLU()(x)

    # Last layer
    d_last = tf.keras.layers.Dense(1, use_bias=True, name='last_layer')
    x = d_last(x)
    x = tf.keras.layers.Activation('sigmoid')(x)

    model = tf.keras.Model(inputs=inp, outputs=x)

    # Set weights
    d1.set_weights([weights['layer1.weight'].T, weights['layer1.bias']])
    ln1.set_weights([weights['layernorm1.weight'], weights['layernorm1.bias']])
    for i in range(n_blocks):
        dense_layers[i+1].set_weights([weights[f'blocks.{i}.fcn_layer.weight'].T, weights[f'blocks.{i}.fcn_layer.bias']])
        norm_layers[i+1].set_weights([weights[f'blocks.{i}.layer_norm.weight'], weights[f'blocks.{i}.layer_norm.bias']])
    d_last.set_weights([weights['last_layer.weight'].T, weights['last_layer.bias']])

    # Convert to TFLite
    converter = tf.lite.TFLiteConverter.from_keras_model(model)
    tflite_model = converter.convert()

    with open(tflite_path, 'wb') as f:
        f.write(tflite_model)

    print(f"TFLite model saved to {tflite_path} ({len(tflite_model) / 1024:.1f} KB)")
    return tflite_path


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--onnx_path', required=True)
    parser.add_argument('--tflite_path', required=True)
    args = parser.parse_args()
    sys.exit(convert(args.onnx_path, args.tflite_path))
