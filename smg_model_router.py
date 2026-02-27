import requests
from enum import Enum

class ModelType(Enum):
    TINYLLAMA = "tinyllama"
    NEURAL_CHAT = "neural-chat"

class SMGModelRouter:
    """Minimal router for SMG model selection"""
    
    def __init__(self, smg_url: str = "http://localhost:30000"):
        self.smg_url = smg_url
        self.chat_endpoint = f"{self.smg_url}/v1/chat/completions"
    
    def route_request(self, prompt: str, max_tokens: int = 100) -> str:
        """Route to model based on prompt length"""
        
        # Simple routing: short → fast, long → quality
        if len(prompt.split()) < 20:
            model = "tinyllama"
        else:
            model = "neural-chat"
        
        print(f"🔀 Routing to {model}")
        
        # Call SMG
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.7
        }
        
        resp = requests.post(self.chat_endpoint, json=payload, timeout=60)
        resp.raise_for_status()
        
        return resp.json()["choices"][0]["message"]["content"]


if __name__ == "__main__":
    router = SMGModelRouter()
    response = router.route_request("What is Python?")
    print(f"Response: {response}")