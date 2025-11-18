#!/bin/bash
# start.sh

echo "🚀 Starting Naino Academy Bot on Render..."
echo "📅 $(date)"
echo "========================================"

# Check if required environment variables are set
if [ -z "$BOT_TOKEN_1" ] || [ -z "$CHAT_ID_1" ] || [ -z "$BOT_TOKEN_2" ] || [ -z "$CHAT_ID_2" ]; then
    echo "❌ ERROR: Missing required environment variables"
    echo "   Please set: BOT_TOKEN_1, CHAT_ID_1, BOT_TOKEN_2, CHAT_ID_2"
    exit 1
fi

echo "✅ Environment variables loaded successfully"

# Start Flask webhook server
exec gunicorn app:app --workers 1 --worker-class sync --bind 0.0.0.0:$PORT
