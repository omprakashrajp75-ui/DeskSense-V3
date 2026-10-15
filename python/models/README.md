# Face detector model files (for gaze scoring)

`vision/opencv_face_gaze.py` needs two small files here — a standard OpenCV
DNN face detector (NOT MediaPipe; ordinary conv-net ops, CPU-safe on this
board). Fetch them once on the board:

```bash
cd python/models
wget -O face_detector.prototxt https://raw.githubusercontent.com/opencv/opencv/master/samples/dnn/face_detector/deploy.prototxt
wget -O face_detector.caffemodel https://github.com/opencv/opencv_3rdparty/raw/dnn_samples_face_detector_20170830/res10_300x300_ssd_iter_140000.caffemodel
```

Total size is under 15MB. If these files are missing, the app still runs —
gaze scoring is disabled and engagement falls back to presence + phone-only
(same graceful-degradation pattern used everywhere else in this project).
