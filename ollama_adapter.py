from flask import Flask, request, jsonify
import requests
import time

app = Flask(__name__)
OLLAMA_BASE = "http://127.0.0.1:11434"

@app.route('/health', methods=['GET'])
def health():
    try:
        requests.get(f"{OLLAMA_BASE}/api/tags", timeout=2)
        return jsonify({"status": "ok"})
    except:
        return jsonify({"status": "error"}), 503

@app.route('/v1/models', methods=['GET'])
def list_models():
    try:
        resp = requests.get(f"{OLLAMA_BASE}/api/tags")
        models = resp.json().get("models", [])
        return jsonify({
            "object": "list",
            "data": [{"id": m["name"], "object": "model"} for m in models]
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/v1/chat/completions', methods=['POST'])
def chat():
    try:
        data = request.json
        model = data.get("model", "tinyllama")
        messages = data.get("messages", [])
        
        # Convert to Ollama format
        prompt = "\n".join([f"{m['role']}: {m['content']}" for m in messages])
        
        resp = requests.post(
            f"{OLLAMA_BASE}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False
            }
        )
        
        result = resp.json()
        return jsonify({
            "id": f"chatcmpl-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": result.get("response", "")
                },
                "finish_reason": "stop"
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    print("Ollama adapter on http://127.0.0.1:8000")
    app.run(host='127.0.0.1', port=8000, debug=False)