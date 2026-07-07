FROM python:3.10-slim

RUN apt-get update && apt-get install -y curl gnupg
RUN curl -fsSL https://deb.nodesource.com/setup_18.x | bash -
RUN apt-get install -y nodejs

WORKDIR /app

# Python deps
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Node deps
COPY package*.json ./
RUN npm install

# Copy backend code (everything in the Root Directory, i.e. the backend folder itself)
COPY . .

EXPOSE 10000 8000

COPY start.sh ./start.sh
RUN chmod +x ./start.sh && sed -i 's/\r//' ./start.sh
CMD ["./start.sh"]