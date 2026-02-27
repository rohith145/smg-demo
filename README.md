![Uploading image.png…]()




Steps to reproduce above:

# 1. Install Homebrew
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 2. Add Homebrew to PATH
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv)"

# 3. Install Rust
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source $HOME/.cargo/env

# 4. Install required tools
brew install git protobuf openssl

# 5. Install Ollama
brew install ollama

# 6. Create projects directory
mkdir -p ~/projects
cd ~/projects

# 7. Clone SMG repository
git clone https://github.com/lightseekorg/smg.git
cd smg

# 8. Set environment variables for OpenSSL
export OPENSSL_DIR=$(brew --prefix openssl)
export LD_LIBRARY_PATH="$(brew --prefix openssl)/lib:$LD_LIBRARY_PATH"

# 9. Build SMG (takes 10-20 minutes first time)
cargo build --release

# 10. Verify build completed
ls -la target/release/smg

# 11. Create adapter that translates Ollama → OpenAI API format
run ollama_adapter.py

# 12. Create Python virtual environment
python3 -m venv ~/venv

# 13. Activate virtual environment
source ~/venv/bin/activate

# 14. Install Python dependencies
pip install flask requests

# 15. Pull Ollama model (one-time)
ollama pull tinyllama


Running the Full Stack (3 Terminals)
Terminal 1: Start Ollama
bash
OLLAMA_HOST=127.0.0.1:11434 ollama serve

# Wait for: "Listening on 127.0.0.1:11434"
Terminal 2: Start Adapter
bash
source ~/venv/bin/activate
python3 ~/ollama_adapter.py

# Wait for: "Ollama adapter on http://127.0.0.1:8000"
Terminal 3: Start SMG Router
bash
cd ~/projects/smg
./target/release/smg launch \
  --host 0.0.0.0 \
  --port 30000 \
  --worker-urls http://127.0.0.1:8000

# Wait for: "INFO: Listening on 0.0.0.0:30000"
Testing SMG
Terminal 4: Test the Setup
bash
# 16. Check models available
curl http://localhost:30000/v1/models

# 17. Send a chat completion request
curl http://localhost:30000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "tinyllama",
    "messages": [{"role": "user", "content": "Hello, what is 2+2?"}],
    "max_tokens": 100
  }'



Python Script to Route Between Two Ollama Models via SMG:

Step 1:
# Terminal: Download two different models
ollama pull tinyllama      # Small, fast model (~1.1B)
ollama pull neural-chat    # Larger, more capable model (~7B)

# Verify both are downloaded
curl http://127.0.0.1:11434/api/tags


Step 2: Run Routing Script
run smg_model_router.py 
