#!/bin/bash
# StonkMonitor — start both backend and frontend

echo "🚀 Starting StonkMonitor..."

# Check for .env
if [ ! -f backend/.env ]; then
  echo "⚠️  No .env found in backend/. Copying from .env.example..."
  cp .env.example backend/.env
  echo "📝 Fill in your API keys in backend/.env then re-run this script."
  exit 1
fi

# Start backend
echo "📡 Starting Python backend..."
cd backend
pip install -r requirements.txt -q
uvicorn main:app --host 127.0.0.1 --port 8000 &
BACKEND_PID=$!
cd ..

# Start frontend
echo "🖥️  Starting Next.js frontend..."
cd frontend
npm install -q
npm run dev &
FRONTEND_PID=$!
cd ..

echo ""
echo "✅ StonkMonitor running!"
echo "   Backend:  http://localhost:8000"
echo "   Frontend: http://localhost:3000"
echo "   API Docs: http://localhost:8000/docs"
echo ""
echo "Press Ctrl+C to stop both"

trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; echo 'Stopped.'" INT
wait
