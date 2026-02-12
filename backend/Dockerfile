# 1. Start with a Python Image (because Data Science libraries are hard to install otherwise)
FROM python:3.10-slim

# 2. Install Node.js (Add Node to the Python image)
RUN apt-get update && apt-get install -y curl gnupg
RUN curl -fsSL https://deb.nodesource.com/setup_18.x | bash -
RUN apt-get install -y nodejs

# 3. Set Working Directory
WORKDIR /app

# 4. Install Python Dependencies (The Brain)
COPY aura_brain/requirements.txt ./aura_brain/requirements.txt
RUN pip install --no-cache-dir -r aura_brain/requirements.txt

# 5. Install Node Dependencies (The Server)
COPY package*.json ./
RUN npm install

# 6. Copy ALL Code
COPY . .

# 7. Expose the ports (Render only respects the main one, but this is good practice)
EXPOSE 10000 8000

# 8. THE STARTUP SCRIPT
# This magic line starts Python in the background (&) and then starts Node
CMD uvicorn aura_brain.brain:app --host 0.0.0.0 --port 8000 & npm start