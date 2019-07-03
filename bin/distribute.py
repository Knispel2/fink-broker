#!/usr/bin/env python
# Copyright 2019 AstroLab Software
# Author: Abhishek Chauhan
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Distribute the alerts to users

1. Use the Alert data that is stored in the Science database (HBase)
2. Apply user defined filters
3. Serialize into Avro
3. Publish to Kafka Topic(s)
"""

import argparse
import json
import time
import os

from fink_broker.parser import getargs
from fink_broker.sparkUtils import init_sparksession
from fink_broker.distributionUtils import get_kafka_df, update_status_in_hbase
from fink_broker.distributionUtils import get_distribution_offset
from fink_broker.distributionUtils import group_df_into_struct
from fink_broker.hbaseUtils import flattenstruct

from fink_broker.filters import filter_df_using_xml, apply_user_defined_filters
from userfilters.leveltwo import filter_leveltwo_names

from pyspark.sql import DataFrame

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    args = getargs(parser)

    # Get or create a Spark Session
    spark = init_sparksession(
        name="distribution", shuffle_partitions=2, log_level="ERROR")

    # Read the catalog file generated by raw2science
    science_db_catalog = args.science_db_catalog
    with open(science_db_catalog) as f:
        catalog = json.load(f)

    # Define variables
    min_timestamp = 100     # set a default
    t_end = 1577836799      # some default value

    # get distribution offset
    min_timestamp = get_distribution_offset(
        args.checkpointpath_dist, args.startingOffset_dist)

    # Run distribution for (args.exit_after) seconds
    if args.exit_after is not None:
        t_end = time.time() + args.exit_after
        exit_after = True
    else:
        exit_after = False

    # Start the distribution service
    while(not exit_after or time.time() < t_end):
        """Keep scanning the HBase for new records in a loop
        """
        # Scan the HBase till current time
        max_timestamp = int(round(time.time() * 1000))  # time in ms

        # Read Hbase within timestamp range
        df = spark.read\
            .option("catalog", catalog)\
            .option("minStamp", min_timestamp)\
            .option("maxStamp", max_timestamp)\
            .format("org.apache.spark.sql.execution.datasources.hbase")\
            .load()

        # Keep records that haven't been distributed
        df = df.filter("status!='distributed'")

        # Apply additional filters (user defined)
        df = filter_df_using_xml(df, args.distribution_rules_xml)

        # group `candidate_*` columns into a struct column
        df = group_df_into_struct(df, "candidate")

        # Apply level two filters
        df = apply_user_defined_filters(df, filter_leveltwo_names)

        # Flatten the struct before distribution
        df = flattenstruct(df, "candidate")

        # Persist df to memory to materialize changes
        df.persist()

        # Get the DataFrame for publishing to Kafka (avro serialized)
        df_kafka = get_kafka_df(df, args.distribution_schema)

        # Publish Kafka topic(s)
        # Ensure that the topic(s) exist on the Kafka Server)
        df_kafka\
            .write\
            .format("kafka")\
            .option("kafka.bootstrap.servers", "localhost:9093")\
            .option("topic", "fink_outstream")\
            .save()

        # Update the status in Hbase and commit checkpoint to file
        update_status_in_hbase(
            df, args.science_db_name, "objectId",
            args.checkpointpath_dist, max_timestamp)

        # update min_timestamp for next iteration
        min_timestamp = max_timestamp

        # free the memory
        df.unpersist()

        # Wait for some time before another loop
        time.sleep(1)


if __name__ == "__main__":
    main()