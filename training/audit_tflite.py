"""Audit TFLite model: accuracy, recall, false positive rate on validation data."""
import sys
import os
import numpy as np

VAL_DIR = '/home/jovyan/Hello_DJ/Hello_DJ'
TFLITE_PATH = '/home/jovyan/Hello_DJ/Hello_DJ.tflite'
FP_VAL_PATH = '/home/jovyan/validation_set_features.npy'
INPUT_SHAPE = (16, 96)


def load_val_data():
    pos = np.load(os.path.join(VAL_DIR, 'positive_features_test.npy'))
    neg = np.load(os.path.join(VAL_DIR, 'negative_features_test.npy'))
    return pos, neg


def load_fp_val_data():
    fp_val = np.load(FP_VAL_PATH)
    n = INPUT_SHAPE[0]
    fp_val = np.array([fp_val[i:i+n] for i in range(0, fp_val.shape[0]-n, 1)])
    return fp_val


def run_audit():
    import tensorflow as tf

    pos, neg = load_val_data()
    n_pos = pos.shape[0]
    n_neg = neg.shape[0]

    X_test = np.vstack((pos, neg)).astype(np.float32)
    y_test = np.hstack((np.ones(n_pos), np.zeros(n_neg)))

    X_fp = load_fp_val_data()
    n_fp_total = len(X_fp)

    # Load TFLite model
    interpreter = tf.lite.Interpreter(model_path=TFLITE_PATH)
    interpreter.allocate_tensors()
    input_details = interpreter.get_input_details()
    output_details = interpreter.get_output_details()
    input_idx = input_details[0]['index']
    output_idx = output_details[0]['index']

    # Accuracy/Recall
    preds = []
    for i in range(len(X_test)):
        interpreter.set_tensor(input_idx, X_test[i:i+1])
        interpreter.invoke()
        out = interpreter.get_tensor(output_idx)[0, 0]
        preds.append(out)
    preds = np.array(preds)
    preds_bin = (preds >= 0.5).astype(float)

    tp = np.sum((preds_bin == 1) & (y_test == 1))
    fn = np.sum((preds_bin == 0) & (y_test == 1))
    fp_test = np.sum((preds_bin == 1) & (y_test == 0))
    tn = np.sum((preds_bin == 0) & (y_test == 0))

    accuracy = (tp + tn) / len(y_test)
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    precision = tp / (tp + fp_test) if (tp + fp_test) > 0 else 0.0

    # FP rate on large validation set
    n_fp_samples = min(50000, n_fp_total)
    X_fp_subset = X_fp[:n_fp_samples]
    fp_preds = []
    for i in range(len(X_fp_subset)):
        interpreter.set_tensor(input_idx, X_fp_subset[i:i+1])
        interpreter.invoke()
        fp_preds.append(interpreter.get_tensor(output_idx)[0, 0])
    fp_preds = np.array(fp_preds)
    fp_count = np.sum(fp_preds >= 0.5)

    val_set_hrs = 11.3
    fp_per_hr = fp_count / (n_fp_samples / n_fp_total * val_set_hrs)

    print(f'Accuracy:  {accuracy:.4f}')
    print(f'Recall:    {recall:.4f}')
    print(f'Precision: {precision:.4f}')
    print(f'FP/hr:     {fp_per_hr:.4f}')
    print(f'TP: {tp}  FN: {fn}  FP(test): {fp_test}  TN: {tn}')
    print(f'FP on validation set: {fp_count} / {n_fp_total} (subsampled {n_fp_samples})')
    return accuracy, recall, fp_per_hr


if __name__ == '__main__':
    run_audit()
