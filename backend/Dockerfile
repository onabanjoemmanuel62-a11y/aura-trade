# 1. Start with a Python Image (because Data Science libraries are hard to install otherwise)
FROM python:3.10-slim

# 2. Install Node.js (Add Node to the Python image)
RUN apt-get update && apt-get install -y curl gnupg
RUN curl -fsSL https://deb.nodesource.com/setup_18.x | bash -
RUN apt-get install -y nodejs

# 3. Set Working Directory
WORKDIR /app

# 4. Install Python Dependencies (The Brain)
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 5. Install Node Dependencies (The Server)
COPY package*.json ./
RUN npm install

# 6. Copy ALL Code
COPY . .

# 7. Expose the ports (Render only respects the main one, but this is good practice)
EXPOSE 10000 8000

# 8. THE STARTUP SCRIPT
# This magic line starts Python in the background (&) and then starts Node
EXPOSE 10000 8000
COPY start.sh ./start.sh
RUN chmod +x ./start.sh
CMD ["./start.sh"]]