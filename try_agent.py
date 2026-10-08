import json
from app.agent import evaluate

data = json.load(open("data/sample_messages.json", encoding="utf-8"))
total_cost = 0.0
correct = 0

for m in data["messages"]:
    r = evaluate(data["product"], m["text"], m["context"])
    is_lead = r["stage"] == "draft"
    correct += is_lead == m["label"]
    total_cost += r["cost"]
    print(
        m["id"], "| stopped at:", r["stage"], "| lead:", is_lead,
        "| expected:", m["label"], "| cost: %.5f" % r["cost"],
        r.get("error", ""),
    )

print("accuracy: %d/%d" % (correct, len(data["messages"])), "| total cost: %.5f" % total_cost)