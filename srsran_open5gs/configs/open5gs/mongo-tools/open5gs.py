import pymongo


class Open5GS:
    def __init__(self, server, port):
        self.server = server
        self.port = port

    def connect_to_mongodb(self):
        client = pymongo.MongoClient(f"mongodb://{self.server}:{self.port}/")
        return client["open5gs"]

    def get_subscribers(self):
        db = self.connect_to_mongodb()
        subscribers = db["subscribers"].find()
        return list(subscribers)

    def add_subscriber(self, sub_data):
        db = self.connect_to_mongodb()
        collection = db["subscribers"]
        sub_data = dict(sub_data)
        sub_data.pop("_id", None)
        sub_data["imsi"] = str(sub_data["imsi"])
        existing = collection.find_one(
            {"imsi": sub_data["imsi"]}, {"_id": 1}
        )
        if existing is None:
            result = collection.insert_one(sub_data)
            print(f"Registered subscriber with ID: {result.inserted_id}")
            return result.inserted_id

        subscriber_id = existing["_id"]
        sub_data["_id"] = subscriber_id
        collection.replace_one({"_id": subscriber_id}, sub_data)
        duplicates = collection.delete_many(
            {
                "imsi": sub_data["imsi"],
                "_id": {"$ne": subscriber_id},
            }
        ).deleted_count
        print(f"Updated subscriber with ID: {subscriber_id}")
        if duplicates:
            print(f"Removed {duplicates} duplicate subscriber records")
        return subscriber_id

    def delete_subscriber(self, imsi):
        db = self.connect_to_mongodb()
        result = db["subscribers"].delete_many({"imsi": str(imsi)})
        return result.deleted_count
