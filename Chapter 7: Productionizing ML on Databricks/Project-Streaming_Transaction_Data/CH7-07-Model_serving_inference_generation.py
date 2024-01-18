# Databricks notebook source
# MAGIC %md
# MAGIC Chapter 7: Productionizing ML on Databricks
# MAGIC
# MAGIC ## Model serving inference generation
# MAGIC We simulate streaming data by generating the labeled JSON data and use the model we deployed to the API to predict the label.

# COMMAND ----------

# MAGIC %md ## Run Setup

# COMMAND ----------

# MAGIC %run ../../global-setup $project_name=synthetic_transactions $env=prod

# COMMAND ----------

# MAGIC %md
# MAGIC ### Databricks Model Serving
# MAGIC This code is given to us in the model serving endpoint UI. 

# COMMAND ----------

import os
import requests
import numpy as np
import pandas as pd
import json

def create_tf_serving_json(data):
  return {'inputs': {name: data[name].tolist() for name in data.keys()} if isinstance(data, dict) else data.tolist()}

def score_model(dataset):
  url = 'https://e2-demo-field-eng.cloud.databricks.com/serving-endpoints/transaction_model/invocations'
  headers = {'Authorization': f'Bearer {os.environ.get("DATABRICKS_TOKEN")}', 
'Content-Type': 'application/json'}
  ds_dict = {'dataframe_split': dataset.to_dict(orient='split')} if isinstance(dataset, pd.DataFrame) else create_tf_serving_json(dataset)
  data_json = json.dumps(ds_dict, allow_nan=True)
  response = requests.request(method='POST', headers=headers, url=url, data=data_json)
  if response.status_code != 200:
    raise Exception(f'Request failed with status {response.status_code}, {response.text}')

  return response.json()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Generate JSON data

# COMMAND ----------

# DBTITLE 1,Data Variables
CustomerID_vars = {"min": 1234, "max": 1260}

Product_vars = {"A": {"min": 1000, "max": 25001, "mean": 15520, "alpha": 4, "beta": 10},
                "B": {"min": 1000, "max": 5501, "mean": 35520, "alpha": 10, "beta": 4},
                "C": {"min": 10000, "max": 40001, "mean": 30520, "alpha": 3, "beta": 10}}

# COMMAND ----------

import dbldatagen as dg
from datetime import datetime
import dbldatagen.distributions as dist
from pyspark.sql.types import IntegerType, FloatType, StringType

def define_specs(Product, Label, currentTimestamp = datetime.now()):
  pVars = Product_vars[Product]
  if Label:
    return (dg.DataGenerator(spark, name="syn_trans", rows=nRows, partitions=4)
      .withColumn("CustomerID", IntegerType(), nullable=False,
                  minValue=CustomerID_vars["min"], maxValue=CustomerID_vars["max"], random=True)
      .withColumn("TransactionTimestamp", "timestamp", 
                  begin=currentTimestamp, end=currentTimestamp,nullable=False,
                random=False)
      .withColumn("Product", StringType(), template=f"Pro\duct \{Product}") 
      .withColumn("Amount", FloatType(), 
                  minValue=pVars["min"],maxValue=pVars["max"], 
                  distribution=dist.Beta(alpha=pVars["alpha"], beta=pVars["beta"]), random=True)
      .withColumn("Label", IntegerType(), minValue=1, maxValue=1)).build()
  else:
    return (dg.DataGenerator(spark, name="syn_transs", rows=nRows, partitions=4)
      .withColumn("CustomerID", IntegerType(), nullable=False,
                  minValue=CustomerID_vars["min"], maxValue=CustomerID_vars["max"], random=True)
      .withColumn("TransactionTimestamp", "timestamp", 
                  begin=currentTimestamp, end=currentTimestamp,nullable=False,
                random=False)
      .withColumn("Product", StringType(), template=f"Pro\duct \{Product}")
      .withColumn("Amount", FloatType(), 
                  minValue=pVars["min"],maxValue=pVars["max"], 
                  distribution=dist.Normal(mean=pVars["mean"], stddev=.001), random=True)
      .withColumn("Label", IntegerType(), minValue=0, maxValue=0)).build()

# COMMAND ----------

# DBTITLE 1,Functions to generate a JSON dataset for Auto Loader to pick up
from pyspark.sql.functions import expr
from functools import reduce
import pyspark
import os

# Generate a record
def generateRecord(Product,Label):
  return (define_specs(Product=Product, Label=Label, currentTimestamp=datetime.now()))
  
# Generate a list of records
def generateRecordSet(Products):
  Labels = [0,1]
  recordSet = []
  for Prod in Products:
    for Lab in Labels:
      recordSet.append(generateRecord(Prod, Lab))
  return reduce(pyspark.sql.dataframe.DataFrame.unionByName, recordSet)


# Generate a set of data, convert it to a Dataframe, write it out as one json file to the temp path. Then move that file to the destination_path
def writeJsonFile(destination_path, Products = list(Product_vars.keys())):
  recordDF = generateRecordSet(Products)
  recordDF = recordDF.withColumn("Amount", expr("Amount / 100"))
  recordDF.coalesce(1).write.format("json").save(temp_path)
  
  # Grab the file from the temp location, write it to the location we want and then delete the temp directory
  tempJson = os.path.join(temp_path, dbutils.fs.ls(temp_path)[3][1])
  dbutils.fs.cp(tempJson, destination_path)
  dbutils.fs.rm(temp_path, True)

# COMMAND ----------

# DBTITLE 1,Loop for Generating Data
from random import randrange
import time

t=1
total = 1000

while(t<total):
  writeJsonFile(destination_path)
  t = t+1
  if not(t%10):
    print(t)
  time.sleep(sleepIntervalSeconds)
  if(t>4000):
    Product_vars = {"A": {"min": 1000, "max": 25001, "mean": 15520, "alpha": 4, "beta": 8},
                    "B": {"min": 1000, "max": 5501, "mean": 35520, "alpha": 8, "beta": 4},
                    "C": {"min": 10000, "max": 40001, "mean": 30520, "alpha": 3, "beta": 8}}

# COMMAND ----------

