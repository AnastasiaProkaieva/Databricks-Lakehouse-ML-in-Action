# Databricks notebook source
!pip install pytorch-lightning==2.1.2 deltalake==0.14.0 deltatorch==0.0.3 evalidate==2.0.2 pillow==10.1.0
dbutils.library.restartPython()

# COMMAND ----------

import os
import numpy as np
import io
import logging
from math import ceil

import torch
from torch import nn
from torch.autograd import Variable
from torch.nn import functional as F
from torch.nn.parallel import DistributedDataParallel
from torch.optim import Optimizer
from torch.utils.data import DataLoader
from torchvision import transforms, models
import pytorch_lightning as pl
from torchmetrics import Accuracy

from pyspark.sql.functions import col
from PIL import Image

import mlflow
from deltatorch import create_pytorch_dataloader, FieldSpec


train_delta_path = "/Volumes/ap/cv_uc/intel_image_clf/train_imgs_main.delta"
val_delta_path = "/Volumes/ap/cv_uc/intel_image_clf/valid_imgs_main.delta"

train_df = (spark.read.format("delta")
            .load(train_delta_path))
            
unique_object_ids = train_df.select("label_name").distinct().collect()
object_id_to_class_mapping = {
    unique_object_ids[idx].label_name: idx for idx in range(len(unique_object_ids))}
object_id_to_class_mapping

# COMMAND ----------

username = spark.sql("SELECT current_user()").first()["current_user()"]
experiment_path = f"/Users/{username}/intel-clf-training_action"
mlflow.set_experiment(experiment_path)

MAIN_DIR_UC = "/Volumes/ap/cv_uc/intel_image_clf/raw_images"
data_dir_Train = f"{MAIN_DIR_UC}/seg_train"
data_dir_Test = f"{MAIN_DIR_UC}/seg_test"
data_dir_pred = f"{MAIN_DIR_UC}/seg_pred/seg_pred"

train_dir = data_dir_Train + "/seg_train"
valid_dir = data_dir_Test + "/seg_test"
pred_files = [os.path.join(data_dir_pred, f) for f in os.listdir(data_dir_pred)]

outcomes = os.listdir(train_dir)

# COMMAND ----------

import os
import torch
from mlflow.store.artifact.models_artifact_repo import ModelsArtifactRepository

best_model = mlflow.search_runs(
    filter_string=f'attributes.status = "FINISHED"',
    order_by=["metrics.train_acc DESC"],
    max_results=10,
).iloc[0]
model_uri = "runs:/{}/model_cv_uc".format(best_model.run_id)

local_path = mlflow.artifacts.download_artifacts(model_uri)
device = "cuda" if torch.cuda.is_available() else "cpu"

requirements_path = os.path.join(local_path, "requirements.txt")
if not os.path.exists(requirements_path):
  dbutils.fs.put("file:" + requirements_path, "", True)

loaded_model = torch.load(local_path+"/data/model.pth", map_location=torch.device(device))
loaded_model

# COMMAND ----------

# MAGIC %md 
# MAGIC ### Score your model on accuracy 
# MAGIC
# MAGIC **Note** this is a demo purpose model, we have not tried to make the accuracy go higher than what it was.  

# COMMAND ----------

import torchvision

transform_tests = torchvision.transforms.Compose([
    transforms.Resize((150,150)),
    transforms.ToTensor(),
    transforms.Normalize((0.425, 0.415, 0.405), (0.255, 0.245, 0.235))
    ])

test_data = torchvision.datasets.ImageFolder(root=valid_dir, transform=transform_tests)
test_loader= DataLoader(test_data, batch_size=32, shuffle=False, num_workers=2)

correct_count, all_count = 0,0
pred_label_list = []
proba_list = []
for images, labels in test_loader:
    for i in range(len(labels)):
        if torch.cuda.is_available():
            images = images.cuda()
            labels = labels.cuda()
        
        img = images[i].view(1,3,150,150)
        with torch.no_grad():
            logps = loaded_model(img)
            
        ps = torch.exp(logps)
        probab = list(ps.cpu()[0])
        proba_list.append(probab)
        pred_label = probab.index(max(probab))
        pred_label_list.append(pred_label)
        true_label = labels.cpu()[i]
        if(true_label == pred_label):
            correct_count += 1
        all_count += 1
        
print("Number of images Tested=", all_count)
print("\n Model Accuracy in % =",(correct_count/all_count)*100)

# COMMAND ----------

import matplotlib.pyplot as plt
from torch.autograd import Variable

transform_tests = torchvision.transforms.Compose([
    transforms.Resize((150,150)),
    transforms.ToTensor(),
    transforms.Normalize((0.425, 0.415, 0.405), (0.255, 0.245, 0.235))
    ])
  
def pred_class(img):
    # transform images
    img_tens = transform_tests(img)
    # change image format (3,150,150) to (1,3,150,150) by help of unsqueeze function
    # image needs to be in cuda before predition
    img_im = img_tens.unsqueeze(0).cuda() 
    uinput = Variable(img_im)
    uinput = uinput.to(device)
    out = loaded_model(uinput)
    # convert image to numpy format in cpu and snatching max prediction score class index
    index = out.data.cpu().numpy().argmax()    
    return index

# COMMAND ----------

classes = {k:v for k , v in enumerate(sorted(outcomes))}
loaded_model.eval()

plt.figure(figsize=(20,20))
for i, images in enumerate(pred_files[:10]):
    # just want 25 images to print
    if i > 24:break
    img = Image.open(images)
    index = pred_class(img)
    plt.subplot(5,5,i+1)
    plt.title(classes[index])
    plt.axis('off')
    plt.imshow(img)

# COMMAND ----------

# MAGIC %md 
# MAGIC ## Score your model to all your images 

# COMMAND ----------

import pandas as pd
from PIL import Image
from torchvision import transforms
import numpy as np
import io
from pyspark.sql.functions import pandas_udf
from typing import Iterator

def feature_extractor(img):
    image = Image.open(io.BytesIO(img))
    transform = transforms.Compose([
    transforms.Resize((150,150)),
    transforms.ToTensor(),
    transforms.Normalize((0.425, 0.415, 0.405), (0.255, 0.245, 0.235))
    ])
    return transform(image)

# to reduce time on loading we broadcast our model to each executor 
model_b = sc.broadcast(loaded_model)

@pandas_udf("struct< label: int, labelName: string>")
def apply_vit(images_iter: Iterator[pd.Series]) -> Iterator[pd.DataFrame]:
    
    model = model_b.value.to(torch.device("cuda"))
    model.eval()
    
    id2label = {0: 'buildings',
                1: 'forest',
                2: 'glacier',
                3: 'mountain',
                4: 'sea',
                5: 'street'}
    
    with torch.set_grad_enabled(False):
        for images in images_iter:
            pil_images = torch.stack(
                [
                    feature_extractor(b)
                    for b in images
                ]
            )
            pil_images = pil_images.to(torch.device("cuda"))
            outputs = model(pil_images)

            preds = torch.max(outputs, 1)[1].tolist()
            probs = torch.nn.functional.softmax(outputs, dim=-1)[:, 1].tolist()
            
            yield pd.DataFrame(
                [
                    {"label": pred, "labelName":id2label[pred]} for pred in preds
                ]
            )

# COMMAND ----------

# with the Brodcasted model we won 40sec, but it's because we do not have a big dataset, in a case of a big set this could significantly speed up things. 
# also take into account that some models may use Batch Inference natively - check API of your Framework. 
# 
spark.conf.set("spark.sql.execution.arrow.maxRecordsPerBatch", 32)
predictions_df = spark.read.format("delta").load("/Volumes/ap/cv_uc/intel_image_clf/valid_imgs_main.delta").withColumn("prediction", apply_vit("content"))
display(predictions_df)

# COMMAND ----------

