import pymongo
from pymongo import UpdateOne, InsertOne, DeleteOne
import json
from bson import ObjectId
import time  # Import time for sleep
import logging
from logging.handlers import RotatingFileHandler
import os

# Set up logging
os.makedirs("logs", exist_ok=True)

logger = logging.getLogger("mongosync")
logger.setLevel(logging.DEBUG)

file_handler = RotatingFileHandler(
    "logs/mongosync.log", maxBytes=5 * 1024 * 1024, backupCount=3
)
file_handler.setLevel(logging.DEBUG)

stream_handler = logging.StreamHandler()
stream_handler.setLevel(logging.INFO)

formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
file_handler.setFormatter(formatter)
stream_handler.setFormatter(formatter)

logger.addHandler(file_handler)
logger.addHandler(stream_handler)


def load_config(filename="config.json"):
    cfg = {}
    if os.path.exists(filename):
        with open(filename) as f:
            cfg = json.load(f)
    return {
        "cluster_uri": cfg.get("cluster_uri") or os.getenv("MONGODB_URI", ""),
        "source_databases": cfg.get("source_databases", ["autoanime480p", "dual"]),
        "source_collection": cfg.get("source_collection", "animes"),
        "target_database": cfg.get("target_database", "progress"),
        "target_collection": cfg.get("target_collection", "pending"),
        "sync_interval": int(cfg.get("sync_interval") or os.getenv("SYNC_INTERVAL", 10)),
    }


def filter_document(doc):
    # Preserve the _id field and define the fields to keep in the document
    return {
        "_id": doc["_id"],  # Ensure the _id field is kept
        "name": doc.get("name"),  # Keep name
        "data": {
            "subtitle": doc.get("data", {}).get("subtitle"),
            "pending": doc.get("data", {}).get("pending"),
            "audio": doc.get("data", {}).get("audio")
        }
    }


def run_sync_cycle(source_collections, target_collection):
    """Run one synchronization pass between source and target collections."""
    # Step 1: Get data from source databases
    all_source_data = []
    for sc in source_collections:
        all_source_data.extend(list(sc.find()))

    # Step 2: Filter data to keep only specific fields
    filtered_source_data = [filter_document(doc) for doc in all_source_data]

    # Step 3: Export filtered data to a JSON file for tracking (optional)
    try:
        with open('temp_data.json', 'w') as f:
            json.dump(filtered_source_data, f, default=str)
    except Exception as e:
        logger.warning(f"Could not write temp_data.json: {e}")

    # Step 4: Process sync logic (Insert, Update, Delete)
    source_dict = {str(doc['_id']): doc for doc in filtered_source_data}
    target_data = list(target_collection.find())
    target_dict = {str(doc['_id']): doc for doc in target_data}

    insertions = []
    updates = []
    deletions = []

    for doc_id, doc in source_dict.items():
        if doc_id in target_dict:
            target_doc = target_dict[doc_id]
            if doc != target_doc:
                updates.append(UpdateOne({'_id': ObjectId(doc_id)}, {'$set': doc}))
                logger.info(f"Document with _id {doc_id} updated")
        else:
            insertions.append(InsertOne(doc))
            logger.info(f"Document with _id {doc_id} inserted")

    for doc_id, target_doc in target_dict.items():
        if doc_id not in source_dict:
            deletions.append(DeleteOne({'_id': ObjectId(doc_id)}))
            logger.info(f"Document with _id {doc_id} deleted")

    if insertions or updates or deletions:
        bulk_ops = insertions + updates + deletions
        result = target_collection.bulk_write(bulk_ops)
        logger.info(f"Bulk write result: {result.bulk_api_result}")
    else:
        logger.info("No changes detected, nothing to sync.")


def main():
    logger.info("Script started")
    initial_config = load_config()

    if not initial_config["cluster_uri"]:
        logger.error("No MongoDB URI configured. Please provide cluster_uri in config.json or set MONGODB_URI.")
        return

    # Connect to MongoDB
    client = pymongo.MongoClient(initial_config["cluster_uri"])

    while True:  # Infinite loop to keep the process running
        sync_interval = 10
        try:
            # Re-read config each cycle so adding source databases takes effect dynamically without restart
            config = load_config()
            sync_interval = config.get("sync_interval", 10)

            source_collection_name = config["source_collection"]
            source_collections = [
                client[db_name][source_collection_name]
                for db_name in config["source_databases"]
            ]

            target_db = client[config["target_database"]]
            target_collection = target_db[config["target_collection"]]

            run_sync_cycle(source_collections, target_collection)
            logger.info("Sync pass completed for sources %s. Waiting %ds...", config["source_databases"], sync_interval)
            time.sleep(sync_interval)
        except Exception:
            logger.exception("Error during sync cycle")
            time.sleep(sync_interval)


if __name__ == "__main__":
    main()
