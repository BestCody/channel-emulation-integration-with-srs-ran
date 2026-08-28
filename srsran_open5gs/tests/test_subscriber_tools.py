import importlib.util
import pathlib
import sys
import types
import unittest
from unittest import mock


MODULE_PATH = (
    pathlib.Path(__file__).parents[1]
    / "configs"
    / "open5gs"
    / "mongo-tools"
    / "open5gs.py"
)
PYMONGO = types.ModuleType("pymongo")
PYMONGO.MongoClient = object
SPEC = importlib.util.spec_from_file_location("subscriber_open5gs", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
with mock.patch.dict(sys.modules, {"pymongo": PYMONGO}):
    SPEC.loader.exec_module(MODULE)


class Result:
    def __init__(self, inserted_id=None, deleted_count=0):
        self.inserted_id = inserted_id
        self.deleted_count = deleted_count


class Collection:
    def __init__(self, documents=None):
        self.documents = list(documents or [])

    def find_one(self, query, projection=None):
        for document in self.documents:
            if document.get("imsi") == query.get("imsi"):
                return {"_id": document["_id"]}
        return None

    def insert_one(self, document):
        document = dict(document)
        document["_id"] = "new-id"
        self.documents.append(document)
        return Result(inserted_id=document["_id"])

    def replace_one(self, query, replacement):
        for index, document in enumerate(self.documents):
            if document["_id"] == query["_id"]:
                self.documents[index] = dict(replacement)
                return
        raise AssertionError("replacement target not found")

    def delete_many(self, query):
        imsi = query["imsi"]
        keep_id = query["_id"]["$ne"]
        kept = [
            document
            for document in self.documents
            if document.get("imsi") != imsi
            or document.get("_id") == keep_id
        ]
        deleted_count = len(self.documents) - len(kept)
        self.documents = kept
        return Result(deleted_count=deleted_count)


class SubscriberToolsTests(unittest.TestCase):
    def receiver(self, collection):
        receiver = MODULE.Open5GS("localhost", 27017)
        receiver.connect_to_mongodb = lambda: {
            "subscribers": collection
        }
        return receiver

    def test_add_inserts_a_new_subscriber(self):
        collection = Collection()

        subscriber_id = self.receiver(collection).add_subscriber(
            {"imsi": 1001, "key": "new"}
        )

        self.assertEqual(subscriber_id, "new-id")
        self.assertEqual(
            collection.documents,
            [{"_id": "new-id", "imsi": "1001", "key": "new"}],
        )

    def test_add_replaces_and_removes_duplicates(self):
        collection = Collection(
            [
                {"_id": "keep", "imsi": "1001", "key": "old"},
                {"_id": "duplicate", "imsi": "1001", "key": "old"},
                {"_id": "other", "imsi": "1002", "key": "other"},
            ]
        )

        subscriber_id = self.receiver(collection).add_subscriber(
            {"imsi": "1001", "key": "updated"}
        )

        self.assertEqual(subscriber_id, "keep")
        self.assertEqual(
            collection.documents,
            [
                {"_id": "keep", "imsi": "1001", "key": "updated"},
                {"_id": "other", "imsi": "1002", "key": "other"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
