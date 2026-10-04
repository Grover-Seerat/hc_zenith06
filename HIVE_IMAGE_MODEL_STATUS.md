# Hive image model status

This build does **not** contain a separate trained Hive Health image checkpoint.
The trained visual checkpoints included in the project are the Bee Health CNN and VERRDO EfficientNet/YOLO models. The Hive Health model in this build is the trained/engineered sensor-history analysis path that combines IoT temperature/humidity history with the latest saved bee-health evidence.

Do not route a hive photograph through the Varroa detector and label it as a Hive Health model. A separate hive-image checkpoint must be supplied before adding true hive-photo inference.
