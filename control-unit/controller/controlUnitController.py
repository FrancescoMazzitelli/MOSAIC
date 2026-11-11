from flask import request, jsonify, Response
from flask_restx import Namespace, Resource
from service.orchestrator import Orchestrator

api = Namespace("control", description="Services management and orchestration")

category_model = api.schema_model(
    "CategorySchema",
    {"type": "object", "additionalProperties": {"type": "string"},
     "example": {"input": "your text here"}},
)


@api.route("/invoke")
@api.expect(category_model)
class ConversationalAgent(Resource):
    def post(self):
        data = request.get_json(force=True)
        user_input = data["input"]
        # max_rank (optional): scopes the catalog for the robustness test.
        # It is propagated down to the gateway's /index/search.
        max_rank = data.get("max_rank")
        # dry_run (optional): planning + validation without execution. Used
        # by the Designer evaluation harness.
        dry_run = bool(data.get("dry_run", False))
        print(f"Received input: {user_input}")

        orchestrator = Orchestrator()
        results = orchestrator.control(user_input, max_rank=max_rank, dry_run=dry_run)
        # control() may return a flask Response (file download case)
        if isinstance(results, Response):
            return results
        return jsonify(results)
