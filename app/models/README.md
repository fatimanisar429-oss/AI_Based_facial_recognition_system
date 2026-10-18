# Models

`face_detection_yunet_2023mar.onnx` (230 KB): the YuNet face detector from the OpenCV Zoo
(https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet), MIT licence.

It is used for ONE thing: telling a covered face (for example a niqab, only the eyes visible) from an empty frame, so the
entry terminal can say "your face is covered, ask the warden to verify you". It never identifies anyone.
