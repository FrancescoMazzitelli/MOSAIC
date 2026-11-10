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
        # max_rank (opzionale): scoping del catalogo per il test di robustezza.
        # Viene propagato fino alla /index/search del gateway.
        max_rank = data.get("max_rank")
        # dry_run (opzionale): planning + validazione senza esecuzione. Usato
        # dall'harness di valutazione del Designer.
        dry_run = bool(data.get("dry_run", False))
        print(f"Input ricevuto: {user_input}")

        orchestrator = Orchestrator()
        results = orchestrator.control(user_input, max_rank=max_rank, dry_run=dry_run)
        # control() puo' restituire una flask Response (caso download file)
        if isinstance(results, Response):
            return results
        return jsonify(results)
