
from pymongo import MongoClient

client = MongoClient("mongodb://localhost:27017")
collection = client["hrone_attendance"]["attendance_logs"]

print(
    "Mongo range count:",
    collection.count_documents({
        "date": {
            "$gte": "2026-07-01",
            "$lte": "2026-07-31",
        }
    }),
)

print(
    "Mongo lower-bound count:",
    collection.count_documents({"date": {"$gte": "2026-07-01"}}),
)

print(
    "Mongo upper-bound count:",
    collection.count_documents({"date": {"$lte": "2026-07-31"}}),
)

client.close()
