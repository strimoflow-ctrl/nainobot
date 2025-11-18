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
echo "🤖 Bot Token 1: ${BOT_TOKEN_1:0:15}..."
echo "👑 Admin ID: $CHAT_ID_1"
echo "📊 Bot Token 2: ${BOT_TOKEN_2:0:15}..."
echo "📍 Data Center Chat ID: $CHAT_ID_2"

# Start the bot
exec python bot.py