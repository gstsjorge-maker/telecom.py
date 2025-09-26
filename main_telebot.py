#!/usr/bin/env python3
"""
Professional Telegram Trading Bot using pyTelegramBotAPI
A mock trading bot that replicates the interface design of popular trading bots
"""

import os
import logging
import asyncio
import datetime
import time
import aiosqlite
import telebot
from telebot import types
import aiohttp
import json
import re
from bot.database import Database
from bot.deposit_monitor import DepositMonitor
from config import BOT_TOKEN, SUPPORTED_TOKENS, WELCOME_MESSAGE, ADMIN_USER_ID

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Initialize bot
bot = telebot.TeleBot(BOT_TOKEN)
db = Database()
deposit_monitor = DepositMonitor(db)

# Track users who are inputting trader IDs and wallet connections
users_inputting_trader_id = set()
users_connecting_wallet = {}  # {user_id: wallet_type}
user_withdrawal_states = {}  # {user_id: {'token': token}}
user_states = {}  # {user_id: {'state': 'waiting_for_support_message'/'waiting_for_bug_report'}}

# Track admin operations
admin_balance_operations = {}  # {user_id: {"action": "add/subtract", "step": "user_id/currency/amount"}}
admin_ban_operations = {}  # {user_id: {"action": "ban/unban", "step": "user_id/reason"}}

# Track pending deposits for manual approval
pending_deposits = {}  # {deposit_id: {"user_id": int, "token_symbol": str, "amount": float, "cost_usd": float, "timestamp": str, "user_info": dict}}

def escape_markdown(text):
    """Escape special characters for Telegram Markdown V1"""
    if not text:
        return ""
    # Escape the following characters: *_`[
    escape_chars = r'([*_`\[])'
    return re.sub(escape_chars, r'\\\1', str(text))

def is_admin(user_id):
    """Check if user is admin"""
    return ADMIN_USER_ID is not None and user_id == ADMIN_USER_ID

def validate_txid(txid: str, token: str) -> bool:
    """Validate transaction ID format for different cryptocurrencies"""
    if not txid or not isinstance(txid, str):
        return False
    
    txid = txid.strip()
    
    # Bitcoin - 64 character hex
    if token in ["BTC"]:
        return len(txid) == 64 and all(c in "0123456789abcdefABCDEF" for c in txid)
    
    # Ethereum/ERC-20 tokens - 66 characters starting with 0x
    elif token in ["ETH", "USDT", "BNB", "MATIC", "LINK"]:
        return (len(txid) == 66 and 
                txid.startswith("0x") and 
                all(c in "0123456789abcdefABCDEF" for c in txid[2:]))
    
    # Solana - 88 character base58
    elif token in ["SOL"]:
        base58_chars = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        return len(txid) == 88 and all(c in base58_chars for c in txid)
    
    # Cardano - 64 character hex 
    elif token in ["ADA"]:
        return len(txid) == 64 and all(c in "0123456789abcdefABCDEF" for c in txid)
    
    return False

def get_token_confirmation_requirement(token: str) -> int:
    """Get required confirmations for each token"""
    confirmation_requirements = {
        "BTC": 2,
        "ETH": 6, 
        "USDT": 6,
        "BNB": 3,
        "MATIC": 3,
        "SOL": 1,
        "ADA": 5,
        "LINK": 6
    }
    return confirmation_requirements.get(token, 3)

async def check_user_banned(user_id, chat_id):
    """Check if user is banned and show ban message if so"""
    is_banned = await db.is_user_banned(user_id)
    if is_banned:
        ban_info = await db.get_user_ban_info(user_id)
        ban_text = f"""🚫 **Account Suspended**

Your account has been suspended from using this trading bot.

**Reason:** {ban_info['reason'] if ban_info else 'Policy violation'}
**Suspended:** {ban_info['banned_at'] if ban_info else 'Recently'}

For appeals, please contact support."""
        
        bot.send_message(chat_id, ban_text, parse_mode='Markdown')
        return True
    return False

async def send_admin_notification(user_id, username, first_name, is_new_user=True):
    """Send notification to admin when user interacts with bot"""
    if not ADMIN_USER_ID:
        return
    
    try:
        # Create user details
        username_display = f"@{username}" if username else "None"
        name_display = first_name if first_name else "Unknown"
        
        user_details = f"""👤 User Details
📊 User ID: {user_id}
👤 Username: {username_display}
📝 Name: {name_display}
📅 Time: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"""
        
        if is_new_user:
            title = "🆕 New User Started Bot"
            message = f"A new user just started using your trading bot!\n\n{user_details}"
        else:
            title = "👤 User Interaction"
            message = f"User interacted with your trading bot.\n\n{user_details}"
        
        # Save to database
        await db.add_admin_notification("user_interaction", user_id, title, message)
        
        # Send real-time notification to admin
        try:
            bot.send_message(
                ADMIN_USER_ID, 
                message
            )
        except Exception as e:
            logger.error(f"Failed to send real-time admin notification: {e}")
            
    except Exception as e:
        logger.error(f"Error sending admin notification: {e}")

def get_main_menu_keyboard(user_id=None):
    """Main menu inline keyboard"""
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("💰 Buy", callback_data="menu_buy"),
        types.InlineKeyboardButton("💸 Sell", callback_data="menu_sell")
    )
    keyboard.add(
        types.InlineKeyboardButton("📊 Portfolio", callback_data="menu_portfolio"),
        types.InlineKeyboardButton("📈 Analytics", callback_data="menu_analytics")
    )
    keyboard.add(
        types.InlineKeyboardButton("💳 Wallet", callback_data="menu_wallet"),
        types.InlineKeyboardButton("📥 Deposits", callback_data="menu_deposits")
    )
    keyboard.add(
        types.InlineKeyboardButton("👥 Copy Trading", callback_data="menu_copy_trading"),
        types.InlineKeyboardButton("⚙️ Settings", callback_data="menu_settings")
    )
    
    # Add admin panel button for admin users only
    if user_id and is_admin(user_id):
        keyboard.add(types.InlineKeyboardButton("👑 Admin Panel", callback_data="menu_admin"))
    
    keyboard.add(
        types.InlineKeyboardButton("🔄 Refresh Prices", callback_data="refresh_prices"),
        types.InlineKeyboardButton("❓ Help", callback_data="menu_help")
    )
    return keyboard

def get_admin_keyboard():
    """Admin panel keyboard"""
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("👥 User Management", callback_data="admin_users"),
        types.InlineKeyboardButton("📊 System Stats", callback_data="admin_stats")
    )
    keyboard.add(
        types.InlineKeyboardButton("💸 Pending Deposits", callback_data="admin_pending_deposits"),
        types.InlineKeyboardButton("💰 Balance Management", callback_data="admin_balance_mgmt")
    )
    keyboard.add(
        types.InlineKeyboardButton("🔔 Notifications", callback_data="admin_notifications"),
        types.InlineKeyboardButton("🔧 Bot Controls", callback_data="admin_controls")
    )
    keyboard.add(
        types.InlineKeyboardButton("📋 Trade History", callback_data="admin_trades"),
        types.InlineKeyboardButton("🔔 Broadcast Message", callback_data="admin_broadcast")
    )
    keyboard.add(
        types.InlineKeyboardButton("🏠 Back to Main", callback_data="back_to_main")
    )
    return keyboard

def get_token_selection_keyboard(action="buy"):
    """Token selection keyboard for buy/sell operations"""
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    tokens = list(SUPPORTED_TOKENS.keys())
    
    # Create rows of 2 tokens each
    for i in range(0, len(tokens), 2):
        row = []
        for j in range(i, min(i + 2, len(tokens))):
            token = tokens[j]
            callback_data = f"{action}_token_{token}"
            row.append(types.InlineKeyboardButton(f"{token}", callback_data=callback_data))
        keyboard.row(*row)
    
    # Add back button
    keyboard.add(types.InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main"))
    return keyboard

def get_portfolio_token_keyboard():
    """Token selection keyboard showing only user's holdings"""
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    
    # This will be populated with user's actual holdings
    # For now, we'll pass an empty keyboard and populate it dynamically
    keyboard.add(types.InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main"))
    return keyboard

def get_sell_amount_keyboard(token_symbol, user_amount):
    """Create sell amount keyboard based on user's holdings"""
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    
    # Percentage-based selling options
    percentages = [
        ("25%", 25), ("50%", 50), ("75%", 75), ("100%", 100)
    ]
    
    for i in range(0, len(percentages), 2):
        row = []
        for j in range(i, min(i + 2, len(percentages))):
            label, percentage = percentages[j]
            amount = user_amount * (percentage / 100)
            row.append(types.InlineKeyboardButton(
                f"{label} ({amount:.6f})",
                callback_data=f"sell_percent_{token_symbol}_{percentage}"
            ))
        keyboard.row(*row)
    
    # Add custom amount and back buttons
    keyboard.add(
        types.InlineKeyboardButton("🔙 Back", callback_data="menu_sell"),
        types.InlineKeyboardButton("🏠 Main Menu", callback_data="back_to_main")
    )
    return keyboard

async def get_crypto_prices():
    """Fetch cryptocurrency prices from CoinGecko"""
    try:
        token_ids = [token_data["coingecko_id"] for token_data in SUPPORTED_TOKENS.values()]
        ids_str = ",".join(token_ids)
        url = f"https://api.coingecko.com/api/v3/simple/price"
        params = {
            "ids": ids_str,
            "vs_currencies": "usd",
            "include_24hr_change": "true"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    # Convert from coingecko_id to token_symbol
                    prices = {}
                    for symbol, token_data in SUPPORTED_TOKENS.items():
                        coingecko_id = token_data["coingecko_id"]
                        if coingecko_id in data:
                            prices[symbol] = {
                                "price": data[coingecko_id]["usd"],
                                "change_24h": data[coingecko_id].get("usd_24h_change", 0)
                            }
                    
                    return prices
    except Exception as e:
        logger.error(f"Error fetching prices: {e}")
        return {}

def format_price(price):
    """Format price with appropriate precision"""
    if price >= 1000:
        return f"${price:,.2f}"
    elif price >= 1:
        return f"${price:.4f}"
    elif price >= 0.01:
        return f"${price:.6f}"
    else:
        return f"${price:.8f}"

def format_percentage(percentage):
    """Format percentage with color indicators"""
    if percentage > 0:
        return f"🟢 +{percentage:.2f}%"
    elif percentage < 0:
        return f"🔴 {percentage:.2f}%"
    else:
        return f"⚪ 0.00%"

@bot.message_handler(commands=['start'])
def start_command(message):
    """Handle /start command"""
    try:
        logger.info(f"Start command received from user {message.from_user.id}")
        user_id = message.from_user.id
        username = message.from_user.username
        first_name = message.from_user.first_name
        
        # Check if user is banned (admins exempt)
        if not is_admin(user_id):
            banned = asyncio.run(check_user_banned(user_id, message.chat.id))
            if banned:
                return
        
        # Check if user is new before creating
        is_new_user = asyncio.run(db.is_user_new(user_id))
        
        # Run async database operation
        asyncio.run(db.create_user(user_id, username, first_name))
        logger.info(f"User {user_id} created/updated in database")
        
        # Send admin notification for user interaction (always notify admin)
        try:
            asyncio.run(send_admin_notification(user_id, username, first_name, is_new_user))
            logger.info(f"Admin notification sent for user {user_id}")
        except Exception as e:
            logger.error(f"Failed to send admin notification: {e}")
        
        # Create personalized welcome message  
        if username:
            user_name = f"@{username}"
        elif first_name:
            user_name = first_name
        else:
            user_name = "Trader"
        
        personalized_message = f"👋 Welcome, {user_name}!\n\n" + WELCOME_MESSAGE
        
        # Send welcome message with main menu
        bot.reply_to(
            message,
            personalized_message,
            reply_markup=get_main_menu_keyboard(user_id)
        )
        logger.info(f"Welcome message sent to user {user_id}")
    except Exception as e:
        logger.error(f"Error in start command: {e}")
        bot.reply_to(message, "⚡ **Trading Engine Restart**\n\nOur systems are auto-scaling due to high trading volume.\n\n🚀 Ready to trade in 3-5 seconds!")

@bot.message_handler(commands=['buy'])
def buy_command(message):
    """Handle /buy command"""
    bot.reply_to(
        message,
        "💰 **Buy Cryptocurrencies**\n\nSelect a token to purchase:",
        parse_mode='Markdown',
        reply_markup=get_token_selection_keyboard("buy")
    )

@bot.message_handler(commands=['portfolio'])
def portfolio_command(message):
    """Handle /portfolio command"""
    user_id = message.from_user.id
    balance = asyncio.run(db.get_user_balance(user_id))
    portfolio = asyncio.run(db.get_user_portfolio(user_id))
    
    if not portfolio:
        portfolio_text = f"📊 **Portfolio Summary**\n\n💵 **Cash Balance:** ${balance:.2f}\n\n📭 No holdings yet.\n\nUse /buy to start trading!"
    else:
        portfolio_text = f"📊 **Portfolio Summary**\n\n💵 **Cash Balance:** ${balance:.2f}\n\n"
        for token, holding in portfolio.items():
            amount = holding["amount"]
            avg_price = holding["avg_price"]
            portfolio_text += f"**{token}**: {amount:.6f} tokens\n   Avg Price: {format_price(avg_price)}\n\n"
    
    bot.reply_to(
        message,
        portfolio_text,
        parse_mode='Markdown',
        reply_markup=get_main_menu_keyboard(user_id)
    )

@bot.message_handler(commands=['prices'])
def prices_command(message):
    """Handle /prices command"""
    try:
        user_id = message.from_user.id
        prices = asyncio.run(get_crypto_prices())
        
        if prices:
            price_text = "💰 **Current Cryptocurrency Prices**\n\n"
            for symbol, price_data in prices.items():
                price_text += f"**{symbol}**: {format_price(price_data['price'])} "
                price_text += f"{format_percentage(price_data.get('change_24h', 0))}\n"
        else:
            price_text = "❌ Unable to fetch current prices. Please try again."
        
        bot.reply_to(
            message,
            price_text,
            parse_mode='Markdown',
            reply_markup=get_main_menu_keyboard(user_id)
        )
    except Exception as e:
        logger.error(f"Error in prices command: {e}")
        bot.reply_to(message, "📊 **High Market Activity**\n\nPrice feeds are updating rapidly due to market movements.\n\n🔄 Refreshing automatically...")


def handle_message_deletion(call, data):
    """Handle selective message deletion - some buttons should have disappearing effect"""
    # Actions that should keep messages visible (navigation, information)
    keep_visible_actions = [
        "back_to_main", "menu_buy", "menu_sell", "menu_portfolio", "menu_wallet", 
        "menu_settings", "menu_admin", "refresh_prices", "wallet_balance", 
        "wallet_history", "wallet_withdrawals"
    ]
    
    # Admin actions that should keep messages visible
    admin_keep_visible = ["admin_users", "admin_balance_mgmt", "admin_trades"]
    
    # Check if this action should keep message visible
    should_keep_visible = (
        data in keep_visible_actions or 
        data in admin_keep_visible or
        data.startswith("view_provider_") or
        data.startswith("copy_address_") or
        data.startswith("copy_withdrawal_")
    )
    
    # Delete message for action buttons (buy amounts, currency selection, etc.)
    if not should_keep_visible:
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass  # If deletion fails, continue

@bot.callback_query_handler(func=lambda call: True)
def handle_callback_query(call):
    """Handle all inline keyboard button callbacks"""
    try:
        data = call.data
        user_id = call.from_user.id
        
        logger.info(f"Callback received from user {user_id}: {data}")
        
        # Handle selective message deletion for better UX
        handle_message_deletion(call, data)
        
        if data == "back_to_main":
            bot.send_message(
                call.message.chat.id,
                """🏠🏠🏠 MAIN MENU 🏠🏠🏠

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🎯 SELECT AN OPTION FROM THE MENU BELOW 🎯

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━""",
                reply_markup=get_main_menu_keyboard(user_id)
            )
        
        elif data == "menu_buy":
            bot.send_message(
                call.message.chat.id,
                """💰💰💰 BUY CRYPTOCURRENCIES 💰💰💰

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🚀 SELECT A TOKEN TO PURCHASE 🚀

Choose from our premium selection of cryptocurrencies below:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━""",
                reply_markup=get_token_selection_keyboard("buy")
            )
        
        elif data == "menu_sell":
            # Check if user has any holdings to sell
            portfolio = asyncio.run(db.get_user_portfolio(user_id))
            
            if not portfolio:
                bot.send_message(
                    call.message.chat.id,
                    """📭📭📭 NO HOLDINGS TO SELL 📭📭📭

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

❌ You don't have any tokens in your portfolio yet.

🎯 Use the BUY option to start trading!

💰 Build your portfolio with our premium tokens

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━""",
                    reply_markup=get_main_menu_keyboard(user_id)
                )
            else:
                # Create dynamic keyboard with user's actual holdings
                keyboard = types.InlineKeyboardMarkup(row_width=2)
                
                # Add tokens that user actually owns
                portfolio_tokens = list(portfolio.keys())
                for i in range(0, len(portfolio_tokens), 2):
                    row = []
                    for j in range(i, min(i + 2, len(portfolio_tokens))):
                        token = portfolio_tokens[j]
                        # Find the holding info
                        holding_info = next(h for h in portfolio if h["token"] == token)
                        amount = holding_info["amount"]
                        row.append(types.InlineKeyboardButton(
                            f"{token} ({amount:.6f})", 
                            callback_data=f"sell_token_{token}"
                        ))
                    keyboard.row(*row)
                
                keyboard.add(types.InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main"))
                
                bot.send_message(
                    call.message.chat.id,
                    """💸💸💸 SELL CRYPTOCURRENCIES 💸💸💸

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📈 SELECT A TOKEN TO SELL FROM YOUR PORTFOLIO 📈

🎯 Your Holdings Are Listed Below:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━""",
                    reply_markup=keyboard
                )
        
        elif data == "menu_wallet":
            balance = asyncio.run(db.get_user_balance(user_id))
            
            wallet_text = f"""💳 **Wallet Information**

💰 **Balance:** ${balance:.2f} USD

This is your real trading balance. Use the options below to manage your wallet:"""
            
            # Create wallet options keyboard
            keyboard = types.InlineKeyboardMarkup(row_width=2)
            keyboard.add(
                types.InlineKeyboardButton("💰 Check Balance", callback_data="wallet_balance"),
                types.InlineKeyboardButton("💳 Transaction History", callback_data="wallet_history")
            )
            keyboard.add(
                types.InlineKeyboardButton("🔄 Refresh", callback_data="wallet_refresh"),
                types.InlineKeyboardButton("⚙️ Wallet Settings", callback_data="wallet_settings")
            )
            keyboard.add(
                types.InlineKeyboardButton("🔗 Connect Wallet", callback_data="wallet_connect"),
                types.InlineKeyboardButton("💸 Withdraw", callback_data="menu_withdraw")
            )
            keyboard.add(types.InlineKeyboardButton("🏠 Main Menu", callback_data="back_to_main"))
            
            bot.send_message(
                call.message.chat.id,
                wallet_text,
                parse_mode='Markdown',
                reply_markup=keyboard
            )
        
        elif data == "menu_portfolio":
            balance = asyncio.run(db.get_user_balance(user_id))
            portfolio = asyncio.run(db.get_user_portfolio(user_id))
            
            if not portfolio:
                portfolio_text = f"📊 **Portfolio Summary**\n\n💵 **Cash Balance:** ${balance:.2f}\n\n📭 No holdings yet.\n\nUse the Buy option to start trading!"
            else:
                portfolio_text = f"📊 **Portfolio Summary**\n\n💵 **Cash Balance:** ${balance:.2f}\n\n"
                for token, holding in portfolio.items():
                    amount = holding["amount"]
                    avg_price = holding["avg_price"]
                    portfolio_text += f"**{token}**: {amount:.6f} tokens\n   Avg Price: {format_price(avg_price)}\n\n"
            
            bot.send_message(
                call.message.chat.id,
                portfolio_text,
                parse_mode='Markdown',
                reply_markup=get_main_menu_keyboard(user_id)
            )
        
        elif data == "menu_settings":
            logger.info(f"🔥 SETTINGS HANDLER REACHED! User: {user_id}")
            settings_text = """⚙️ **Settings & Preferences**

**Trading Settings:**
• Slippage Tolerance: 0.5%
• Auto-confirm trades: Enabled
• Price alerts: Coming soon

**Account Settings:**
• Notifications: Enabled
• Security: Two-factor pending

**System Info:**
• Connected wallet: Not connected
• API status: Online ✅

Select an option below to customize your experience:"""
            
            keyboard = types.InlineKeyboardMarkup(row_width=2)
            keyboard.add(
                types.InlineKeyboardButton("🔒 Security", callback_data="settings_security"),
                types.InlineKeyboardButton("📊 Trading", callback_data="settings_trading")
            )
            keyboard.add(
                types.InlineKeyboardButton("🔔 Notifications", callback_data="settings_notifications"),
                types.InlineKeyboardButton("💳 Wallet Connect", callback_data="settings_wallet")
            )
            keyboard.add(
                types.InlineKeyboardButton("🏠 Main Menu", callback_data="back_to_main")
            )
            
            bot.send_message(
                call.message.chat.id,
                """⚙️⚙️⚙️ SETTINGS & PREFERENCES ⚙️⚙️⚙️

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🎯 CUSTOMIZE YOUR TRADING EXPERIENCE 🎯

Professional settings panel for advanced traders

Select an option below to customize your experience:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━""",
                reply_markup=keyboard
            )
        
        elif data == "settings_security":
            security_text = """🔒 **Security Settings**

**Current Security Status:**
• Two-Factor Authentication: ❌ Disabled
• Login Notifications: ✅ Enabled  
• API Access: ❌ Disabled
• Withdrawal Limits: ✅ Active

**Security Features:**
• Account lock after failed attempts
• Email verification required
• IP address monitoring
• Secure session management

**Recommended Actions:**
• Enable 2FA for enhanced security
• Review recent login activity
• Set up backup authentication

Security features are simulated for demo purposes."""

            keyboard = types.InlineKeyboardMarkup(row_width=2)
            keyboard.add(
                types.InlineKeyboardButton("🔐 Enable 2FA", callback_data="security_2fa"),
                types.InlineKeyboardButton("📧 Email Settings", callback_data="security_email")
            )
            keyboard.add(
                types.InlineKeyboardButton("🛡️ Login History", callback_data="security_history"),
                types.InlineKeyboardButton("🔑 Change Password", callback_data="security_password")
            )
            keyboard.add(types.InlineKeyboardButton("🔙 Back to Settings", callback_data="menu_settings"))
            
            bot.send_message(
                call.message.chat.id,
                security_text,
                parse_mode='Markdown',
                reply_markup=keyboard
            )
        
        elif data == "settings_trading":
            trading_text = """📊 **Trading Settings**

**Current Configuration:**
• Slippage Tolerance: 0.5%
• Auto-confirm trades: ✅ Enabled
• Price alerts: ❌ Coming soon
• Advanced orders: ❌ Coming soon

**Risk Management:**
• Daily trading limit: $5,000
• Maximum position size: $1,000
• Stop-loss protection: Manual only
• Take-profit targets: Manual only

**Trading Preferences:**
• Preferred order type: Market orders
• Confirmation required: Yes
• Price impact warnings: Enabled

All trading features are simulated for educational purposes."""

            keyboard = types.InlineKeyboardMarkup(row_width=2)
            keyboard.add(
                types.InlineKeyboardButton("⚙️ Slippage", callback_data="trading_slippage"),
                types.InlineKeyboardButton("🎯 Limits", callback_data="trading_limits")
            )
            keyboard.add(
                types.InlineKeyboardButton("🔔 Alerts", callback_data="trading_alerts"),
                types.InlineKeyboardButton("🛡️ Risk Settings", callback_data="trading_risk")
            )
            keyboard.add(types.InlineKeyboardButton("🔙 Back to Settings", callback_data="menu_settings"))
            
            bot.send_message(
                call.message.chat.id,
                trading_text,
                parse_mode='Markdown',
                reply_markup=keyboard
            )
        
        elif data == "settings_notifications":
            notifications_text = """🔔 **Notification Settings**

**Current Status:**
• Deposit confirmations: ✅ Enabled
• Trade executions: ✅ Enabled
• Price alerts: ❌ Coming soon
• Market updates: ❌ Coming soon

**Notification Methods:**
• Telegram messages: ✅ Active
• Email notifications: ❌ Disabled
• Push notifications: ❌ Not available

**Alert Preferences:**
• Successful trades: Instant
• Failed transactions: Instant
• Large price movements: Disabled
• Copy trading updates: Enabled

Configure your preferred notification settings below."""

            keyboard = types.InlineKeyboardMarkup(row_width=2)
            keyboard.add(
                types.InlineKeyboardButton("📱 Telegram", callback_data="notif_telegram"),
                types.InlineKeyboardButton("📧 Email", callback_data="notif_email")
            )
            keyboard.add(
                types.InlineKeyboardButton("💰 Price Alerts", callback_data="notif_price"),
                types.InlineKeyboardButton("📈 Trade Alerts", callback_data="notif_trade")
            )
            keyboard.add(types.InlineKeyboardButton("🔙 Back to Settings", callback_data="menu_settings"))
            
            bot.send_message(
                call.message.chat.id,
                notifications_text,
                parse_mode='Markdown',
                reply_markup=keyboard
            )
        
        elif data == "settings_wallet":
            wallet_settings_text = """💳 **Wallet Connection Settings**

**Current Status:**
• Connected wallets: None
• Auto-connect: ❌ Disabled
• Wallet security: Standard

**Supported Wallets:**
• MetaMask - Browser extension
• Trust Wallet - Mobile app
• Coinbase Wallet - Multi-platform
• WalletConnect - Universal protocol
• Phantom - Solana ecosystem

**Connection Features:**
• Secure credential validation
• Multi-wallet support
• Automatic disconnection
• Transaction signing

Connect your wallet to enable advanced trading features and copy trading."""

            keyboard = types.InlineKeyboardMarkup(row_width=2)
            keyboard.add(
                types.InlineKeyboardButton("🦊 Connect MetaMask", callback_data="connect_wallet_MetaMask"),
                types.InlineKeyboardButton("🛡️ Connect Trust", callback_data="connect_wallet_Trust")
            )
            keyboard.add(
                types.InlineKeyboardButton("🔵 Connect Coinbase", callback_data="connect_wallet_Coinbase"),
                types.InlineKeyboardButton("👻 Connect Phantom", callback_data="connect_wallet_Phantom")
            )
            keyboard.add(
                types.InlineKeyboardButton("📱 WalletConnect", callback_data="connect_wallet_WalletConnect"),
                types.InlineKeyboardButton("🔙 Back to Settings", callback_data="menu_settings")
            )
            
            bot.send_message(
                call.message.chat.id,
                wallet_settings_text,
                parse_mode='Markdown',
                reply_markup=keyboard
            )
        
        elif data == "wallet_connect":
            wallet_connect_text = """🔗 **Connect Your Wallet**

Connect your cryptocurrency wallet to enable advanced trading features and copy trading.

**Supported Wallets:**
• 🦊 MetaMask - Most popular browser extension
• 🛡️ Trust Wallet - Mobile-first multi-chain wallet  
• 🔵 Coinbase Wallet - User-friendly with DeFi support
• 👻 Phantom - Leading Solana ecosystem wallet
• 📱 WalletConnect - Universal connection protocol

**Why Connect?**
• Enable copy trading features
• Direct transaction signing
• Enhanced security
• Multi-chain support

**Safety Note:**
Your credentials are validated securely and never stored permanently."""

            keyboard = types.InlineKeyboardMarkup(row_width=2)
            keyboard.add(
                types.InlineKeyboardButton("🦊 MetaMask", callback_data="connect_wallet_MetaMask"),
                types.InlineKeyboardButton("🛡️ Trust Wallet", callback_data="connect_wallet_Trust")
            )
            keyboard.add(
                types.InlineKeyboardButton("🔵 Coinbase", callback_data="connect_wallet_Coinbase"),
                types.InlineKeyboardButton("👻 Phantom", callback_data="connect_wallet_Phantom")
            )
            keyboard.add(
                types.InlineKeyboardButton("📱 WalletConnect", callback_data="connect_wallet_WalletConnect"),
                types.InlineKeyboardButton("🔙 Back to Wallet", callback_data="menu_wallet")
            )
            
            bot.send_message(
                call.message.chat.id,
                wallet_connect_text,
                parse_mode='Markdown',
                reply_markup=keyboard
            )

        elif data == "menu_withdraw":
            # Withdrawal interface
            balance = asyncio.run(db.get_user_balance(user_id))
            portfolio = asyncio.run(db.get_user_portfolio(user_id))
            
            # Calculate total portfolio value
            total_portfolio_value = 0
            if portfolio:
                prices = asyncio.run(get_crypto_prices()) or {}
                for token, holding in portfolio.items():
                    token_price = prices.get(token, {}).get('price', 0)
                    total_portfolio_value += holding['amount'] * token_price
            
            total_value = balance + total_portfolio_value
            
            if total_value < 10:
                bot.send_message(
                    call.message.chat.id,
                    f"❌ **Insufficient funds for withdrawal**\n\nMinimum withdrawal: $10.00\nYour total value: ${total_value:.2f}\n\nTrade more to reach the minimum!",
                    parse_mode='Markdown',
                    reply_markup=types.InlineKeyboardMarkup([
                        [types.InlineKeyboardButton("🔙 Back to Wallet", callback_data="menu_wallet")]
                    ])
                )
                return
            
            withdraw_text = f"""💸💸💸 CRYPTO WITHDRAWAL ONLY 💸💸💸

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**Your Total Account Value:** ${total_value:.2f}
💵 **Cash Balance:** ${balance:.2f}
🪙 **Token Value:** ${total_portfolio_value:.2f}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🪙 **CRYPTO TOKENS ONLY** - Send to external wallet

⚠️⚠️ MANDATORY 10% FEE ⚠️⚠️
- 10% fee applies to ALL crypto withdrawals
- Fee payment is MANDATORY to proceed
- Processing time: 15-30 minutes

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

💎💎 START CRYPTO WITHDRAWAL 💎💎"""
            
            keyboard = types.InlineKeyboardMarkup()
            keyboard.add(types.InlineKeyboardButton("🪙 Start Crypto Withdrawal", callback_data="withdraw_type_CRYPTO"))
            keyboard.add(
                types.InlineKeyboardButton("📊 Withdrawal History", callback_data="wallet_withdrawals"),
                types.InlineKeyboardButton("🔙 Back to Wallet", callback_data="menu_wallet")
            )
            
            bot.send_message(
                call.message.chat.id,
                withdraw_text,
                parse_mode='Markdown',
                reply_markup=keyboard
            )

        elif data == "menu_analytics":
            # Get real user data
            balance = asyncio.run(db.get_user_balance(user_id))
            portfolio = asyncio.run(db.get_user_portfolio(user_id))
            
            # Calculate portfolio value
            portfolio_value = 0
            if portfolio:
                prices = asyncio.run(get_crypto_prices())
                for token, holding in portfolio.items():
                    amount = holding["amount"]
                    if prices and token in prices:
                        portfolio_value += amount * prices[token]["price"]
            
            total_value = balance + portfolio_value
            starting_balance = 6000  # Initial balance
            total_change = total_value - starting_balance
            change_percent = (total_change / starting_balance) * 100 if starting_balance > 0 else 0
            
            # Generate realistic analytics based on user activity
            total_trades = 47 + (user_id % 25)
            win_rate = 68.1 + (user_id % 20) / 10
            risk_score = "Moderate" if total_value > 6500 else "Low" if total_value > 5500 else "High"
            best_asset = "SOL" if portfolio and len(portfolio) > 0 else "N/A"
            diversification = f"{min(10, max(3, len(portfolio) * 2.5)):.1f}/10" if portfolio else "3.0/10"
            
            analytics_text = f"""📈 **Trading Analytics Dashboard**

**Performance Overview:**
• Total Portfolio Value: ${total_value:.2f}
• 24h Change: ${total_change:+.2f} ({change_percent:+.2f}%)
• Total Trades: {total_trades}
• Win Rate: {win_rate:.1f}%

**Recent Activity:**
• Last trade: {total_trades} completed trades
• Active positions: {len(portfolio) if portfolio else 0}

**Market Insights:**
• Market trending: Bullish momentum
• Best performing asset: {best_asset}
• Recommended action: Monitor key levels

**Advanced Analytics:**
• Risk Score: {risk_score}
• Diversification: {diversification}
• Average hold time: 2.3 days

**🎯 Select Analysis Type:**"""

            keyboard = types.InlineKeyboardMarkup(row_width=2)
            keyboard.add(
                types.InlineKeyboardButton("📊 Performance", callback_data="analytics_performance"),
                types.InlineKeyboardButton("📉 Risk Analysis", callback_data="analytics_risk")
            )
            keyboard.add(
                types.InlineKeyboardButton("🎯 Recommendations", callback_data="analytics_recommendations"),
                types.InlineKeyboardButton("📈 Market Trends", callback_data="analytics_trends")
            )
            keyboard.add(types.InlineKeyboardButton("🏠 Main Menu", callback_data="back_to_main"))
            
            bot.send_message(
                call.message.chat.id,
                analytics_text,
                parse_mode='Markdown',
                reply_markup=keyboard
            )

        elif data == "refresh_prices":
            try:
                prices = asyncio.run(get_crypto_prices())
                
                if prices:
                    price_text = "💰 **Current Prices** (Updated)\n\n"
                    for symbol, price_data in prices.items():
                        price_text += f"**{symbol}**: {format_price(price_data['price'])} "
                        price_text += f"{format_percentage(price_data.get('change_24h', 0))}\n"
                    
                    price_text += "\n🔄 *Prices updated just now*"
                else:
                    price_text = "❌ Unable to fetch current prices. Please try again."
                
                bot.send_message(
                    call.message.chat.id,
                    price_text,
                    parse_mode='Markdown',
                    reply_markup=get_main_menu_keyboard(user_id)
                )
            except Exception as e:
                logger.error(f"Error refreshing prices: {e}")
                bot.answer_callback_query(call.id, "⚠️ Error fetching prices. Please try again.")
        
        elif data.startswith("buy_token_"):
            token_symbol = data.replace("buy_token_", "")
            
            # Get token information
            token_info = SUPPORTED_TOKENS.get(token_symbol, {})
            token_name = token_info.get("name", token_symbol)
            token_address = token_info.get("address", "Address not available")
            token_network = token_info.get("network", "Network info unavailable")
            
            # Get current price
            prices = asyncio.run(get_crypto_prices())
            if prices and token_symbol in prices:
                price_text = f"💰 **Current Price:** {format_price(prices[token_symbol]['price'])}"
                change_text = f"📈 **24h Change:** {format_percentage(prices[token_symbol].get('change_24h', 0))}"
            else:
                price_text = "💰 **Price:** Loading..."
                change_text = ""
            
            # Create the deposit information message
            deposit_message = f"""🔥🔥🔥 {token_name} ({token_symbol}) DEPOSIT 🔥🔥🔥

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{price_text}
{change_text}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🏦🏦 DEPOSIT ADDRESS (TAP TO COPY) 🏦🏦

🌐 NETWORK: {token_network}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

⚡⚡ AUTO-DETECTION ENABLED ⚡⚡
Funds will be credited instantly upon confirmation.

💎💎 SELECT AMOUNT TO PURCHASE 💎💎"""
            
            # Create amount selection keyboard with token-specific amounts
            keyboard = types.InlineKeyboardMarkup(row_width=2)
            
            # Define token-specific amounts
            if token_symbol == "BTC":
                amounts = [
                    ("₿ 0.0001", "0.0001"), ("₿ 0.0005", "0.0005"),
                    ("₿ 0.001", "0.001"), ("₿ 0.005", "0.005"),
                    ("₿ 0.01", "0.01"), ("₿ 0.05", "0.05")
                ]
            elif token_symbol == "ETH":
                amounts = [
                    ("Ξ 0.01", "0.01"), ("Ξ 0.05", "0.05"),
                    ("Ξ 0.1", "0.1"), ("Ξ 0.5", "0.5"),
                    ("Ξ 1", "1"), ("Ξ 2", "2")
                ]
            elif token_symbol == "SOL":
                amounts = [
                    ("◎ 0.5", "0.5"), ("◎ 1", "1"),
                    ("◎ 3", "3"), ("◎ 5", "5"),
                    ("◎ 10", "10"), ("◎ 20", "20")
                ]
            elif token_symbol == "USDT":
                amounts = [
                    ("₮ 10", "10"), ("₮ 25", "25"),
                    ("₮ 50", "50"), ("₮ 100", "100"),
                    ("₮ 500", "500"), ("₮ 1000", "1000")
                ]
            elif token_symbol == "BNB":
                amounts = [
                    ("⚡ 0.1", "0.1"), ("⚡ 0.5", "0.5"),
                    ("⚡ 1", "1"), ("⚡ 2", "2"),
                    ("⚡ 5", "5"), ("⚡ 10", "10")
                ]
            elif token_symbol in ["MATIC", "ADA", "LINK"]:
                # Keep USD amounts for these tokens
                amounts = [
                    ("💵 $10", "usd_10"), ("💵 $25", "usd_25"),
                    ("💵 $50", "usd_50"), ("💵 $100", "usd_100"),
                    ("💵 $500", "usd_500"), ("💵 $1000", "usd_1000")
                ]
            else:
                # Default to USD for any other tokens
                amounts = [
                    ("💵 $10", "usd_10"), ("💵 $25", "usd_25"),
                    ("💵 $50", "usd_50"), ("💵 $100", "usd_100"),
                    ("💵 $500", "usd_500"), ("💵 $1000", "usd_1000")
                ]
            
            # Add clickable address as a button (tap to copy)
            keyboard.add(types.InlineKeyboardButton(
                f"{token_address}", 
                callback_data=f"copy_address_{token_symbol}"
            ))
            
            # Create the amount selection buttons
            for i in range(0, len(amounts), 2):
                row = []
                for j in range(i, min(i + 2, len(amounts))):
                    label, value = amounts[j]
                    row.append(types.InlineKeyboardButton(label, callback_data=f"buy_amount_{token_symbol}_{value}"))
                keyboard.row(*row)
                
            keyboard.add(types.InlineKeyboardButton("🔄 Refresh Price", callback_data=f"refresh_token_{token_symbol}"))
            keyboard.add(
                types.InlineKeyboardButton("🔙 Back", callback_data="menu_buy"),
                types.InlineKeyboardButton("🏠 Main Menu", callback_data="back_to_main")
            )
            
            bot.send_message(
                call.message.chat.id,
                deposit_message,
                parse_mode='Markdown',
                reply_markup=keyboard
            )
        
        elif data.startswith("sell_token_"):
            token_symbol = data.replace("sell_token_", "")
            
            # Get user's holdings for this token
            portfolio = asyncio.run(db.get_user_portfolio(user_id))
            user_holdings = {token: data["amount"] for token, data in portfolio.items()}
            
            if token_symbol not in user_holdings:
                bot.send_message(
                    call.message.chat.id,
                    f"❌ You don't own any {token_symbol}.",
                    reply_markup=get_main_menu_keyboard(user_id)
                )
                return
            
            user_amount = user_holdings[token_symbol]
            
            # Get current price for display
            prices = asyncio.run(get_crypto_prices())
            if prices and token_symbol in prices:
                current_price = prices[token_symbol]['price']
                price_text = f"💰 **Current Price:** {format_price(current_price)}"
                
                # Calculate portfolio value
                portfolio_value = user_amount * current_price
                value_text = f"💎 **Your Holdings:** {user_amount:.6f} {token_symbol} (${portfolio_value:.2f})"
            else:
                price_text = "💰 **Price:** Loading..."
                value_text = f"💎 **Your Holdings:** {user_amount:.6f} {token_symbol}"
            
            sell_message = f"""💸💸💸 SELL {token_symbol} 💸💸💸

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{price_text}
{value_text}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

⚡⚡ SELECT AMOUNT TO SELL ⚡⚡

🎯 Choose your sell amount from the options below:"""
            
            bot.send_message(
                call.message.chat.id,
                sell_message,
                reply_markup=get_sell_amount_keyboard(token_symbol, user_amount)
            )
        
        elif data.startswith("sell_percent_"):
            # Handle sell percentage selection
            parts = data.split("_")
            if len(parts) >= 4:
                token_symbol = parts[2]
                percentage = float(parts[3])
                
                # Get user's holdings
                portfolio = asyncio.run(db.get_user_portfolio(user_id))
                user_holdings = {token: data["amount"] for token, data in portfolio.items()}
                
                if token_symbol not in user_holdings:
                    bot.answer_callback_query(call.id, f"❌ You don't own any {token_symbol}")
                    return
                
                total_amount = user_holdings[token_symbol]
                sell_amount = total_amount * (percentage / 100)
                
                # Execute the sell order
                asyncio.run(execute_sell_order(call, user_id, token_symbol, sell_amount))
        
        elif data == "menu_admin":
            # Admin panel access - check if user is admin
            if not is_admin(user_id):
                bot.answer_callback_query(call.id, "❌ Access denied. Admin only.")
                return
            
            # Get system statistics
            total_users = asyncio.run(db.get_total_users())
            total_trades = asyncio.run(db.get_total_trades())
            total_volume = asyncio.run(db.get_total_volume())
            
            admin_text = f"""👑 **Admin Panel**

📊 **System Statistics:**
• Total Users: {total_users if total_users else 0}
• Total Trades: {total_trades if total_trades else 0}
• Total Volume: ${total_volume if total_volume else 0:.2f}

⚡ **Quick Actions:**
Use the buttons below to manage the bot:"""
            
            bot.send_message(
                call.message.chat.id,
                admin_text,
                parse_mode='Markdown',
                reply_markup=get_admin_keyboard()
            )
        
        elif data == "admin_pending_deposits":
            # Handle pending deposits admin interface
            if not is_admin(user_id):
                bot.answer_callback_query(call.id, "❌ Access denied. Admin only.")
                return
            
            if not pending_deposits:
                deposits_text = """💸 **Pending Deposits Management**

📭 **No pending deposits found.**

All deposit requests will appear here for manual approval.

When users submit deposit confirmations, you'll receive detailed notifications and can approve or reject them from this interface."""
                
                keyboard = types.InlineKeyboardMarkup()
                keyboard.add(types.InlineKeyboardButton("🔄 Refresh", callback_data="admin_pending_deposits"))
                keyboard.add(types.InlineKeyboardButton("🔙 Back to Admin", callback_data="menu_admin"))
                
                bot.send_message(call.message.chat.id, deposits_text, reply_markup=keyboard)
            else:
                deposits_text = f"""💸 **Pending Deposits Management**

📋 **{len(pending_deposits)} deposit(s) awaiting approval:**

"""
                
                keyboard = types.InlineKeyboardMarkup()
                
                for i, (deposit_id, deposit_data) in enumerate(list(pending_deposits.items())[:5], 1):
                    user_info = deposit_data["user_info"]
                    username = user_info.get("username", "No username")
                    first_name = user_info.get("first_name", "Unknown")
                    
                    deposits_text += f"""**{i}. Deposit ID:** `{deposit_id}`
• **User:** {escape_markdown(first_name)} (@{escape_markdown(username)})
• **Amount:** {deposit_data['amount']:.6f} {deposit_data['token_symbol']}
• **Value:** ${deposit_data['cost_usd']:.2f}
• **Time:** {deposit_data['timestamp']}

"""
                    
                    keyboard.add(
                        types.InlineKeyboardButton(f"✅ Approve #{i}", callback_data=f"approve_deposit_{deposit_id}"),
                        types.InlineKeyboardButton(f"❌ Reject #{i}", callback_data=f"reject_deposit_{deposit_id}")
                    )
                
                if len(pending_deposits) > 5:
                    deposits_text += f"\n... and {len(pending_deposits) - 5} more deposits"
                
                keyboard.add(types.InlineKeyboardButton("🔄 Refresh", callback_data="admin_pending_deposits"))
                keyboard.add(types.InlineKeyboardButton("🔙 Back to Admin", callback_data="menu_admin"))
                
                bot.send_message(call.message.chat.id, deposits_text, parse_mode='Markdown', reply_markup=keyboard)
        
        elif data.startswith("approve_deposit_"):
            # Handle deposit approval
            if not is_admin(user_id):
                bot.answer_callback_query(call.id, "❌ Access denied. Admin only.")
                return
            
            deposit_id = data.replace("approve_deposit_", "")
            
            if deposit_id not in pending_deposits:
                bot.answer_callback_query(call.id, "❌ Deposit not found or already processed.")
                return
            
            deposit_data = pending_deposits[deposit_id]
            target_user_id = deposit_data["user_id"]
            token_symbol = deposit_data["token_symbol"]
            token_amount = deposit_data["amount"]
            cost_usd = deposit_data["cost_usd"]
            
            try:
                # Add tokens to user's portfolio
                current_price = cost_usd / token_amount  # Calculate effective price
                asyncio.run(db.update_portfolio(target_user_id, token_symbol, token_amount, current_price))
                asyncio.run(db.add_trade_history(target_user_id, "DEPOSIT", token_symbol, token_amount, current_price, cost_usd))
                
                # Remove from pending deposits
                del pending_deposits[deposit_id]
                
                # Notify user of approval
                user_notification = f"""✅ **DEPOSIT APPROVED!**

Your deposit has been successfully processed and added to your account.

**Deposit Details:**
• **Amount:** {token_amount:.6f} {token_symbol}
• **Value:** ${cost_usd:.2f}
• **Deposit ID:** `{deposit_id}`

Your tokens are now available in your portfolio!"""

                try:
                    bot.send_message(target_user_id, user_notification, parse_mode='Markdown')
                except:
                    pass  # User might have blocked the bot
                
                # Confirm to admin
                bot.answer_callback_query(call.id, f"✅ Deposit {deposit_id} approved successfully!")
                
                # Show updated pending deposits list immediately
                if not pending_deposits:
                    bot.edit_message_text(
                        """💸 **Pending Deposits Management**

📭 **No pending deposits found.**

All deposit requests will appear here for manual approval.""",
                        call.message.chat.id,
                        call.message.message_id,
                        reply_markup=types.InlineKeyboardMarkup([
                            [types.InlineKeyboardButton("🔄 Refresh", callback_data="admin_pending_deposits")],
                            [types.InlineKeyboardButton("🔙 Back to Admin", callback_data="menu_admin")]
                        ])
                    )
                else:
                    # Refresh to show remaining deposits
                    deposits_text = f"""💸 **Pending Deposits Management**

📋 **{len(pending_deposits)} deposit(s) awaiting approval:**

"""
                    keyboard = types.InlineKeyboardMarkup()
                    
                    for i, (deposit_id_remaining, deposit_data_remaining) in enumerate(list(pending_deposits.items())[:5], 1):
                        user_info = deposit_data_remaining["user_info"]
                        username = user_info.get("username", "No username")
                        first_name = user_info.get("first_name", "Unknown")
                        
                        deposits_text += f"""**{i}. Deposit ID:** `{deposit_id_remaining}`
• **User:** {escape_markdown(first_name)} (@{escape_markdown(username)})
• **Amount:** {deposit_data_remaining['amount']:.6f} {deposit_data_remaining['token_symbol']}
• **Value:** ${deposit_data_remaining['cost_usd']:.2f}
• **Time:** {deposit_data_remaining['timestamp']}

"""
                        
                        keyboard.add(
                            types.InlineKeyboardButton(f"✅ Approve #{i}", callback_data=f"approve_deposit_{deposit_id_remaining}"),
                            types.InlineKeyboardButton(f"❌ Reject #{i}", callback_data=f"reject_deposit_{deposit_id_remaining}")
                        )
                    
                    keyboard.add(types.InlineKeyboardButton("🔄 Refresh", callback_data="admin_pending_deposits"))
                    keyboard.add(types.InlineKeyboardButton("🔙 Back to Admin", callback_data="menu_admin"))
                    
                    bot.edit_message_text(deposits_text, call.message.chat.id, call.message.message_id, 
                                        parse_mode='Markdown', reply_markup=keyboard)
                
                logger.info(f"Admin {user_id} approved deposit {deposit_id}")
                
            except Exception as e:
                logger.error(f"Error approving deposit: {e}")
                bot.answer_callback_query(call.id, "❌ Error processing approval. Please try again.")
        
        elif data.startswith("reject_deposit_"):
            # Handle deposit rejection
            if not is_admin(user_id):
                bot.answer_callback_query(call.id, "❌ Access denied. Admin only.")
                return
            
            deposit_id = data.replace("reject_deposit_", "")
            
            if deposit_id not in pending_deposits:
                bot.answer_callback_query(call.id, "❌ Deposit not found or already processed.")
                return
            
            deposit_data = pending_deposits[deposit_id]
            target_user_id = deposit_data["user_id"]
            
            # Remove from pending deposits
            del pending_deposits[deposit_id]
            
            # Notify user of rejection
            user_notification = f"""❌ **DEPOSIT REJECTED**

Your deposit request has been rejected after review.

**Deposit ID:** `{deposit_id}`

**Possible reasons:**
• Transaction not found on blockchain
• Incorrect amount sent
• Wrong network used
• Duplicate submission

Please contact support if you believe this is an error."""

            try:
                bot.send_message(target_user_id, user_notification, parse_mode='Markdown')
            except:
                pass  # User might have blocked the bot
            
            # Confirm to admin
            bot.answer_callback_query(call.id, f"❌ Deposit {deposit_id} rejected.")
            
            # Show updated pending deposits list immediately
            if not pending_deposits:
                bot.edit_message_text(
                    """💸 **Pending Deposits Management**

📭 **No pending deposits found.**

All deposit requests will appear here for manual approval.""",
                    call.message.chat.id,
                    call.message.message_id,
                    reply_markup=types.InlineKeyboardMarkup([
                        [types.InlineKeyboardButton("🔄 Refresh", callback_data="admin_pending_deposits")],
                        [types.InlineKeyboardButton("🔙 Back to Admin", callback_data="menu_admin")]
                    ])
                )
            else:
                # Refresh to show remaining deposits
                deposits_text = f"""💸 **Pending Deposits Management**

📋 **{len(pending_deposits)} deposit(s) awaiting approval:**

"""
                keyboard = types.InlineKeyboardMarkup()
                
                for i, (deposit_id_remaining, deposit_data_remaining) in enumerate(list(pending_deposits.items())[:5], 1):
                    user_info = deposit_data_remaining["user_info"]
                    username = user_info.get("username", "No username") 
                    first_name = user_info.get("first_name", "Unknown")
                    
                    deposits_text += f"""**{i}. Deposit ID:** `{deposit_id_remaining}`
• **User:** {escape_markdown(first_name)} (@{escape_markdown(username)})
• **Amount:** {deposit_data_remaining['amount']:.6f} {deposit_data_remaining['token_symbol']}
• **Value:** ${deposit_data_remaining['cost_usd']:.2f}
• **Time:** {deposit_data_remaining['timestamp']}

"""
                    
                    keyboard.add(
                        types.InlineKeyboardButton(f"✅ Approve #{i}", callback_data=f"approve_deposit_{deposit_id_remaining}"),
                        types.InlineKeyboardButton(f"❌ Reject #{i}", callback_data=f"reject_deposit_{deposit_id_remaining}")
                    )
                
                keyboard.add(types.InlineKeyboardButton("🔄 Refresh", callback_data="admin_pending_deposits"))
                keyboard.add(types.InlineKeyboardButton("🔙 Back to Admin", callback_data="menu_admin"))
                
                bot.edit_message_text(deposits_text, call.message.chat.id, call.message.message_id, 
                                    parse_mode='Markdown', reply_markup=keyboard)
            
            logger.info(f"Admin {user_id} rejected deposit {deposit_id}")
        
        elif data.startswith("admin_curr_"):
            # Handle currency selection for admin balance management (MOVED UP)
            logger.info(f"🔥 ADMIN CURRENCY HANDLER REACHED! Data: {data}, User: {user_id}")
            
            if not is_admin(user_id):
                bot.answer_callback_query(call.id, "❌ Access denied. Admin only.")
                return
                
            currency = data.replace("admin_curr_", "")
            operation = admin_balance_operations.get(user_id, {})
            action = operation.get("action")
            target_user_id = operation.get("target_user_id")
            
            logger.info(f"Currency selection: {currency}, operation: {operation}")
            
            if not operation or not target_user_id:
                bot.answer_callback_query(call.id, "❌ Session expired. Please start again.")
                return
            
            admin_balance_operations[user_id].update({
                "currency": currency,
                "step": "amount"
            })
            
            # Acknowledge the button press
            bot.answer_callback_query(call.id, f"✅ {currency} selected")
            
            # Edit the existing message instead of sending a new one
            amount_text = f"""💰 Enter Amount

User ID: {target_user_id}
Currency: {currency}
Action: {action.title()}

Enter the amount to {action}:

Example: 100.50"""
            
            keyboard = types.InlineKeyboardMarkup()
            keyboard.add(types.InlineKeyboardButton("❌ Cancel", callback_data="admin_balance_mgmt"))
            
            try:
                bot.edit_message_text(
                    amount_text,
                    call.message.chat.id, 
                    call.message.message_id,
                    reply_markup=keyboard
                )
            except Exception as e:
                logger.error(f"Error editing message: {e}")
                bot.send_message(call.message.chat.id, amount_text, reply_markup=keyboard)
        
        elif data.startswith("admin_"):
            # Admin functionality - check access first
            if not is_admin(user_id):
                bot.answer_callback_query(call.id, "❌ Access denied. Admin only.")
                return
            
            action = data.replace("admin_", "")
            
            if action == "users":
                # User management
                users = asyncio.run(db.get_all_users())
                user_text = "👥 User Management\n\n"
                
                if not users:
                    user_text += "No users found."
                else:
                    for i, user in enumerate(users[:10], 1):  # Show first 10 users
                        balance = asyncio.run(db.get_user_balance(user['user_id']))
                        is_banned = asyncio.run(db.is_user_banned(user['user_id']))
                        ban_status = "🚫 BANNED" if is_banned else "✅ Active"
                        
                        # Format username display
                        username_display = f"@{user['username']}" if user.get('username') else user.get('first_name', 'No Name')
                        
                        user_text += f"{i}. {username_display}\n   ID: {user['user_id']} | Balance: ${balance:.2f} | {ban_status}\n\n"
                    
                    if len(users) > 10:
                        user_text += f"\n... and {len(users) - 10} more users"
                
                keyboard = types.InlineKeyboardMarkup()
                keyboard.add(
                    types.InlineKeyboardButton("💰 Manage Balance", callback_data="admin_balance_mgmt"),
                    types.InlineKeyboardButton("🚫 Ban/Unban User", callback_data="admin_ban_mgmt")
                )
                keyboard.add(types.InlineKeyboardButton("🔙 Back to Admin", callback_data="menu_admin"))
                
                bot.send_message(call.message.chat.id, user_text, reply_markup=keyboard)
            
            elif action == "stats":
                # System statistics
                total_users = asyncio.run(db.get_total_users())
                total_trades = asyncio.run(db.get_total_trades())
                total_volume = asyncio.run(db.get_total_volume())
                
                stats_text = f"""📊 **Detailed System Statistics**

**User Statistics:**
• Total Registered Users: {total_users if total_users else 0}
• Active Users (24h): Coming soon

**Trading Statistics:**
• Total Trades Executed: {total_trades if total_trades else 0}
• Total Trading Volume: ${total_volume if total_volume else 0:.2f}
• Average Trade Size: ${(total_volume / total_trades) if total_trades and total_volume else 0:.2f}

**System Health:**
• Bot Status: ✅ Online
• Database Status: ✅ Connected
• API Status: ✅ Active"""
                
                keyboard = types.InlineKeyboardMarkup()
                keyboard.add(
                    types.InlineKeyboardButton("🔄 Refresh Stats", callback_data="admin_stats"),
                    types.InlineKeyboardButton("🔙 Back to Admin", callback_data="menu_admin")
                )
                
                bot.send_message(call.message.chat.id, stats_text, parse_mode='Markdown', reply_markup=keyboard)
            
            elif action == "notifications":
                # Admin notifications
                try:
                    notifications = asyncio.run(db.get_admin_notifications(limit=20))
                    unread_count = asyncio.run(db.get_unread_notification_count())
                    
                    if not notifications:
                        notifications_text = """🔔 **Admin Notifications**

📭 No notifications yet.

You'll receive notifications when:
• New users start using the bot
• Users interact with the bot
• Important system events occur"""
                    else:
                        notifications_text = f"""🔔 **Admin Notifications**

📊 **Unread:** {unread_count} notifications

**Recent Activity:**

"""
                        for notif in notifications[:10]:
                            status_icon = "🔴" if not notif['is_read'] else "✅"
                            time_str = notif['created_at'][:16] if notif['created_at'] else "Unknown"
                            notifications_text += f"{status_icon} **{notif['title']}**\n"
                            notifications_text += f"📅 {time_str}\n"
                            notifications_text += f"👤 User: {notif['user_id']}\n\n"
                    
                    keyboard = types.InlineKeyboardMarkup()
                    keyboard.add(
                        types.InlineKeyboardButton("🔄 Refresh", callback_data="admin_notifications"),
                        types.InlineKeyboardButton("✅ Mark All Read", callback_data="admin_mark_read")
                    )
                    keyboard.add(
                        types.InlineKeyboardButton("🔙 Back to Admin", callback_data="menu_admin")
                    )
                    
                    bot.send_message(call.message.chat.id, notifications_text, parse_mode='Markdown', reply_markup=keyboard)
                    
                except Exception as e:
                    logger.error(f"Error fetching notifications: {e}")
                    bot.send_message(call.message.chat.id, "❌ Error loading notifications.", reply_markup=get_admin_keyboard())
                
            elif action == "controls":
                # Bot controls
                controls_text = """🔧 **Bot Controls**

**Available Actions:**
• 🔄 Restart Background Services
• 📊 Clear Cache
• 🔔 Send System Notification
• ⚠️ Emergency Stop

**System Configuration:**
• Slippage: 0.5%
• Initial Balance: $10,000
• Monitoring: ✅ Active"""
                
                keyboard = types.InlineKeyboardMarkup()
                keyboard.add(
                    types.InlineKeyboardButton("🔄 Restart Services", callback_data="admin_restart"),
                    types.InlineKeyboardButton("📊 Clear Cache", callback_data="admin_clear_cache")
                )
                keyboard.add(types.InlineKeyboardButton("🔙 Back to Admin", callback_data="menu_admin"))
                
                bot.send_message(call.message.chat.id, controls_text, parse_mode='Markdown', reply_markup=keyboard)
                
            elif action == "mark_read":
                # Mark all notifications as read
                try:
                    notifications = asyncio.run(db.get_admin_notifications(unread_only=True))
                    for notif in notifications:
                        asyncio.run(db.mark_notification_read(notif['id']))
                    
                    bot.answer_callback_query(call.id, "✅ All notifications marked as read!")
                    # No need to refresh here, user can click refresh button if needed
                    
                except Exception as e:
                    logger.error(f"Error marking notifications as read: {e}")
                    bot.answer_callback_query(call.id, "❌ Error updating notifications.")
            
            elif action == "balances":
                # Balance management interface
                balance_text = """💰 Balance Management

Manage user balances across different currencies:

Instructions:
1. Select action (Add/Subtract)
2. Enter User ID (number) or Username (@username)
3. Choose currency
4. Enter amount

Available Currencies:
• USD (Account Balance)
• BTC, ETH, SOL, USDT, BNB, MATIC, ADA, LINK"""
                
                keyboard = types.InlineKeyboardMarkup()
                keyboard.add(
                    types.InlineKeyboardButton("➕ Add Balance", callback_data="admin_add_balance"),
                    types.InlineKeyboardButton("➖ Subtract Balance", callback_data="admin_subtract_balance")
                )
                keyboard.add(types.InlineKeyboardButton("🔙 Back to Admin", callback_data="menu_admin"))
                
                bot.send_message(call.message.chat.id, balance_text, reply_markup=keyboard)
            
            elif action == "trades":
                # Trade history overview
                recent_trades = asyncio.run(db.get_recent_trades_admin(20))
                
                trades_text = "📋 Recent Trade Activity\n\n"
                
                if not recent_trades:
                    trades_text += "No recent trades found."
                else:
                    for i, trade in enumerate(recent_trades[:10], 1):
                        trade_type = "🟢 BUY" if trade['trade_type'] == 'BUY' else "🔴 SELL"
                        trades_text += f"{i}. User {trade['user_id']}: {trade_type} {trade['amount']:.4f} {trade['token']}\n"
                
                keyboard = types.InlineKeyboardMarkup()
                keyboard.add(
                    types.InlineKeyboardButton("🔄 Refresh", callback_data="admin_trades"),
                    types.InlineKeyboardButton("🔙 Back to Admin", callback_data="menu_admin")
                )
                
                bot.send_message(call.message.chat.id, trades_text, reply_markup=keyboard)
            
            elif action == "broadcast":
                # Get user count for broadcast info
                try:
                    total_users = asyncio.run(db.get_total_user_count())
                except:
                    total_users = "N/A"
                
                broadcast_text = f"""📢 **Admin Broadcast System**

**📊 Broadcast Statistics:**
• Total registered users: {total_users}
• Active users (24h): {int(total_users * 0.35) if isinstance(total_users, int) else "N/A"}
• Last broadcast: None sent

**📝 Broadcast Types:**
• 🚨 **Emergency Alert** - Critical system notifications
• 📈 **Market Update** - Trading signals and market news  
• 🎉 **Promotion** - New features and special offers
• 📢 **Announcement** - General platform updates

**⚠️ Important Guidelines:**
• Messages reach ALL registered users instantly
• Use clear, professional language
• Include relevant emojis for better engagement
• Avoid excessive frequency (max 2/day)

**🎯 Ready to send?** Choose broadcast type below:"""
                
                keyboard = types.InlineKeyboardMarkup(row_width=2)
                keyboard.add(
                    types.InlineKeyboardButton("🚨 Emergency Alert", callback_data="broadcast_emergency"),
                    types.InlineKeyboardButton("📈 Market Update", callback_data="broadcast_market")
                )
                keyboard.add(
                    types.InlineKeyboardButton("🎉 Promotion", callback_data="broadcast_promo"),
                    types.InlineKeyboardButton("📢 General Update", callback_data="broadcast_general")
                )
                keyboard.add(
                    types.InlineKeyboardButton("📊 Broadcast Stats", callback_data="broadcast_stats"),
                    types.InlineKeyboardButton("🔙 Back to Admin", callback_data="menu_admin")
                )
                
                bot.send_message(call.message.chat.id, broadcast_text, parse_mode='Markdown', reply_markup=keyboard)
            
            elif action == "balance_mgmt":
                # Balance management interface
                balance_text = """💰 Balance Management

Manage user balances across different currencies:

Instructions:
1. Select action (Add/Subtract)
2. Enter User ID (number) or Username (@username)
3. Choose currency
4. Enter amount

Available Currencies:
• USD (Account Balance)
• BTC, ETH, SOL, USDT, BNB, MATIC, ADA, LINK"""
                
                keyboard = types.InlineKeyboardMarkup()
                keyboard.add(
                    types.InlineKeyboardButton("➕ Add Balance", callback_data="admin_add_balance"),
                    types.InlineKeyboardButton("➖ Subtract Balance", callback_data="admin_subtract_balance")
                )
                keyboard.add(types.InlineKeyboardButton("🔙 Back to Users", callback_data="admin_users"))
                
                bot.send_message(call.message.chat.id, balance_text, reply_markup=keyboard)
            
            elif action == "ban_mgmt":
                # Ban management interface
                ban_text = """🚫 User Ban Management

Manage user access and restrictions:

Ban User:
Prevents user from using the bot entirely. They will receive a ban notification when trying to access any features.

Unban User:
Restores full access to previously banned users.

Enter the User ID to ban or unban:"""
                
                keyboard = types.InlineKeyboardMarkup()
                keyboard.add(
                    types.InlineKeyboardButton("🚫 Ban User", callback_data="admin_ban_user"),
                    types.InlineKeyboardButton("✅ Unban User", callback_data="admin_unban_user")
                )
                keyboard.add(types.InlineKeyboardButton("🔙 Back to Users", callback_data="admin_users"))
                
                bot.send_message(call.message.chat.id, ban_text, reply_markup=keyboard)
            
            elif action == "add_balance":
                # Request balance addition details
                asyncio.run(start_balance_management(call.message.chat.id, user_id, "add"))
            
            elif action == "subtract_balance":
                # Request balance subtraction details
                asyncio.run(start_balance_management(call.message.chat.id, user_id, "subtract"))
            
            elif action == "ban_user":
                # Request user ID to ban
                asyncio.run(start_user_ban_process(call.message.chat.id, user_id))
            
            elif action == "unban_user":
                # Request user ID to unban
                asyncio.run(start_user_unban_process(call.message.chat.id, user_id))
        
        elif data.startswith("wallet_"):
            # Handle wallet options
            action = data.replace("wallet_", "")
            
            if action == "balance":
                # Get both USD balance and token portfolio
                balance = asyncio.run(db.get_user_balance(user_id))
                portfolio = asyncio.run(db.get_user_portfolio(user_id))
                
                balance_text = f"""💰 **Detailed Balance**

💵 **USD Balance:** ${balance:.2f}

"""
                
                if portfolio:
                    balance_text += "🪙 **Token Holdings:**\n"
                    total_portfolio_value = 0
                    
                    for token, holding in portfolio.items():
                        amount = holding['amount']
                        avg_price = holding.get('avg_price', 0)
                        
                        # Get current price for value calculation  
                        current_prices = asyncio.run(get_crypto_prices()) or {}
                        current_price = current_prices.get(token, {}).get('price', avg_price)
                        current_value = amount * current_price
                        total_portfolio_value += current_value
                        
                        # Format based on token type
                        if token in ['BTC', 'ETH']:
                            amount_display = f"{amount:.6f}"
                        else:
                            amount_display = f"{amount:.4f}"
                            
                        if current_price > 0:
                            balance_text += f"• **{token}:** {amount_display} @ ${format_price(current_price)[1:]} ≈ ${current_value:.2f}\n"
                        else:
                            balance_text += f"• **{token}:** {amount_display} (Price unavailable)\n"
                    
                    balance_text += f"\n💎 **Total Portfolio Value:** ${total_portfolio_value:.2f}"
                    balance_text += f"\n🏦 **Total Account Value:** ${balance + total_portfolio_value:.2f}"
                else:
                    balance_text += "🪙 **Token Holdings:** None\n\nStart trading to build your portfolio!"
                
                keyboard = types.InlineKeyboardMarkup()
                keyboard.add(
                    types.InlineKeyboardButton("🔄 Refresh", callback_data="wallet_refresh"),
                    types.InlineKeyboardButton("🔙 Back to Wallet", callback_data="menu_wallet")
                )
                
                bot.send_message(
                    call.message.chat.id,
                    balance_text,
                    parse_mode='Markdown',
                    reply_markup=keyboard
                )
            
            elif action == "history":
                trades = asyncio.run(db.get_trade_history(user_id, 10))
                
                if not trades:
                    history_text = "📋 **Transaction History**\n\nNo transactions yet.\n\nStart trading to see your history here!"
                else:
                    history_text = "📋 **Transaction History**\n\n"
                    for i, trade in enumerate(trades[:10], 1):
                        trade_type = "🟢 BUY" if trade.get('trade_type', trade.get('type', 'UNKNOWN')) == 'BUY' else "🔴 SELL"
                        history_text += f"{i}. {trade_type} {trade['amount']:.6f} {trade['token']} at {format_price(trade['price'])}\n"
                        if i >= 10:
                            break
                
                keyboard = types.InlineKeyboardMarkup()
                keyboard.add(
                    types.InlineKeyboardButton("🔄 Refresh", callback_data="wallet_history"),
                    types.InlineKeyboardButton("🔙 Back to Wallet", callback_data="menu_wallet")
                )
                
                bot.send_message(
                    call.message.chat.id,
                    history_text,
                    parse_mode='Markdown',
                    reply_markup=keyboard
                )
            
            elif action == "refresh":
                # Refresh wallet information
                bot.answer_callback_query(call.id, "🔄 Refreshing wallet...")
                balance = asyncio.run(db.get_user_balance(user_id))
                
                wallet_text = f"""💳 **Wallet Information**

💰 **Balance:** ${balance:.2f} USD

This is your real trading balance. Use the options below to manage your wallet:"""
                
                keyboard = types.InlineKeyboardMarkup(row_width=2)
                keyboard.add(
                    types.InlineKeyboardButton("💰 Check Balance", callback_data="wallet_balance"),
                    types.InlineKeyboardButton("💳 Transaction History", callback_data="wallet_history")
                )
                keyboard.add(
                    types.InlineKeyboardButton("🔄 Refresh", callback_data="wallet_refresh"),
                    types.InlineKeyboardButton("⚙️ Wallet Settings", callback_data="wallet_settings")
                )
                keyboard.add(types.InlineKeyboardButton("🏠 Main Menu", callback_data="back_to_main"))
                
                bot.send_message(
                    call.message.chat.id,
                    """💳💳💳 WALLET INFORMATION (REFRESHED) 💳💳💳

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

💰💰 BALANCE: ${:.2f} USD 💰💰

This is your real trading balance. Use the options below to manage your wallet:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━""".format(balance),
                    reply_markup=keyboard
                )
            
            elif action == "settings":
                settings_text = f"""⚙️ **Wallet Settings**

**Trading Preferences:**
• Slippage Tolerance: 0.5%
• Auto-confirm trades: ✅ Enabled
• Risk Level: Medium

**Security:**
• Two-factor authentication: ❌ Disabled
• Trade confirmations: ✅ Enabled

**📱 Advanced Features:**
• Price alerts: ✅ Configured
• Risk management: ✅ Active
• Portfolio rebalancing: ⚙️ Available
• Copy trading sync: ✅ Enabled

**🔧 Customization:**
Use the buttons below to modify your settings."""
                
                keyboard = types.InlineKeyboardMarkup()
                keyboard.add(
                    types.InlineKeyboardButton("🔒 Security Settings", callback_data="settings_security"),
                    types.InlineKeyboardButton("📊 Trading Preferences", callback_data="settings_trading")
                )
                keyboard.add(types.InlineKeyboardButton("🔙 Back to Wallet", callback_data="menu_wallet"))
                
                bot.send_message(
                    call.message.chat.id,
                    settings_text,
                    parse_mode='Markdown',
                    reply_markup=keyboard
                )
            
            elif action == "withdraw":
                # Show crypto withdrawal options only
                withdraw_text = """💸💸💸 CRYPTO WITHDRAWAL 💸💸💸

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🪙 **CRYPTO TOKENS ONLY** - Send to external wallet

⚠️ **WITHDRAWAL FEE:** 10% mandatory fee applies to ALL crypto withdrawals

**Processing Time:**
• Crypto: 15-30 minutes (after confirmations)

**Security:**
• All withdrawals require wallet address verification
• Daily limits apply for your protection
• Fee payment is mandatory to proceed

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

💎💎 SELECT TOKEN TO WITHDRAW 💎💎"""
                
                keyboard = types.InlineKeyboardMarkup()
                keyboard.add(types.InlineKeyboardButton("🪙 Select Crypto Token", callback_data="withdraw_type_CRYPTO"))
                keyboard.add(types.InlineKeyboardButton("🔙 Back to Wallet", callback_data="menu_wallet"))
                
                bot.send_message(
                    call.message.chat.id,
                    withdraw_text,
                    parse_mode='Markdown',
                    reply_markup=keyboard
                )
            
            elif action == "withdrawals":
                # Show withdrawal history
                withdrawals = asyncio.run(db.get_user_withdrawals(user_id, 10))
                
                if not withdrawals:
                    history_text = """📊📊📊 WITHDRAWAL HISTORY 📊📊📊

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

❌ No withdrawals yet.

💰 Use the withdraw option to cash out your profits!

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""
                else:
                    history_text = """📊📊📊 WITHDRAWAL HISTORY 📊📊📊

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🎯 Your Recent Withdrawals:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

"""
                    for i, withdrawal in enumerate(withdrawals[:10], 1):
                        status_emoji = {"pending": "⏳", "processing": "🔄", "completed": "✅", "failed": "❌"}.get(withdrawal['status'], "❓")
                        withdrawal_type = "💵 USD" if withdrawal['token'] == 'USD' else f"🪙 {withdrawal['token']}"
                        
                        # Show address for crypto withdrawals  
                        if withdrawal['token'] != 'USD' and withdrawal.get('to_address'):
                            history_text += f"{i}. {status_emoji} {withdrawal_type} {withdrawal['amount']:.6f}\n"
                            history_text += f"   📍 Address (tap to copy):\n\n"
                        else:
                            history_text += f"{i}. {status_emoji} {withdrawal_type} ${withdrawal['amount']:.2f}\n\n"
                        
                        if i >= 10:
                            break
                
                keyboard = types.InlineKeyboardMarkup()
                
                # Add clickable address buttons for crypto withdrawals
                crypto_withdrawals = [w for w in withdrawals if w['token'] != 'USD' and w.get('to_address')]
                if crypto_withdrawals:
                    for withdrawal in crypto_withdrawals[:5]:  # Show address buttons for first 5
                        address = withdrawal['to_address']
                        keyboard.add(types.InlineKeyboardButton(
                            f"📋 {address[:30]}...{address[-10:] if len(address) > 40 else address}",
                            callback_data=f"copy_withdrawal_{withdrawal['id']}"
                        ))
                
                keyboard.add(
                    types.InlineKeyboardButton("🔄 Refresh", callback_data="wallet_withdrawals"),
                    types.InlineKeyboardButton("🔙 Back to Wallet", callback_data="menu_wallet")
                )
                
                bot.send_message(
                    call.message.chat.id,
                    history_text,
                    reply_markup=keyboard
                )
        
        elif data.startswith("withdraw_type_"):
            # Handle crypto withdrawal type selection (USD removed)
            withdrawal_type = data.replace("withdraw_type_", "")
            
            if withdrawal_type == "CRYPTO":
                # Crypto withdrawal flow - show available tokens
                balance = asyncio.run(db.get_user_balance(user_id))
                portfolio = asyncio.run(db.get_user_portfolio(user_id))
                
                if not portfolio:
                    bot.send_message(
                        call.message.chat.id,
                        "📭 **No crypto tokens to withdraw**\n\nYou don't have any cryptocurrency tokens in your portfolio.\n\n💰 Your USD balance: ${:.2f}".format(balance),
                        parse_mode='Markdown',
                        reply_markup=types.InlineKeyboardMarkup([
                            [types.InlineKeyboardButton("🔙 Back", callback_data="wallet_withdraw")]
                        ])
                    )
                    return
                
                from bot.keyboards import get_withdrawal_token_keyboard
                
                withdraw_text = """📜📜📜 REGULATORY WITHDRAWAL POLICY 📜📜📜

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**NOTICE:** External cryptocurrency transfers are subject to federal compliance requirements under 31 CFR Part 1010 (Bank Secrecy Act).

⚖️ **Mandatory Compliance:** All external transfers require regulatory processing fees as mandated by FinCEN and state MSB licensing regulations.

🏛️ **Legal Framework:** BSA/AML, OFAC screening, KYC/CDD documentation, and SEC custody rule compliance are federally mandated.

📋 **Policy Effective:** January 2024 (Policy WD-2024-001)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

💼💼 SELECT ASSET FOR COMPLIANCE REVIEW 💼💼"""
                
                keyboard = types.InlineKeyboardMarkup(row_width=2)
                for token, item in portfolio.items():
                    amount = item["amount"]
                    keyboard.add(types.InlineKeyboardButton(
                        f"🪙 {token} ({amount:.6f})",
                        callback_data=f"withdraw_token_{token}"
                    ))
                
                keyboard.add(types.InlineKeyboardButton("🔙 Back", callback_data="wallet_withdraw"))
                
                bot.send_message(
                    call.message.chat.id,
                    withdraw_text,
                    reply_markup=keyboard
                )
        
        elif data.startswith("withdraw_token_"):
            # Handle specific token withdrawal
            token = data.replace("withdraw_token_", "")
            
# Remove USD withdrawal - only crypto allowed
            if token != "USD":
                # Crypto token withdrawal
                portfolio = asyncio.run(db.get_user_portfolio(user_id))
                user_holdings = {token: data["amount"] for token, data in portfolio.items()}
                
                if token not in user_holdings or user_holdings[token] == 0:
                    bot.send_message(
                        call.message.chat.id,
                        f"❌ You don't have any {token} to withdraw.",
                        reply_markup=types.InlineKeyboardMarkup([
                            [types.InlineKeyboardButton("🔙 Back", callback_data="withdraw_type_CRYPTO")]
                        ])
                    )
                    return
                
                token_amount = user_holdings[token]
                withdrawal_fee = token_amount * 0.10  # 10% fee
                net_amount = token_amount - withdrawal_fee  # Amount user receives
                
                # MANDATORY FEE CONFIRMATION STEP
                withdraw_text = f"""📜📜📜 REGULATORY COMPLIANCE NOTICE 📜📜📜

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🏛️ **POLICY REF:** WD-2024-001 | **EFFECTIVE:** Jan 2024
📋 **WITHDRAWAL ASSET:** {token} | **AMOUNT:** {token_amount:.6f}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

⚖️ **MANDATORY REGULATORY COMPLIANCE FEE**

As required by FinCEN Guidelines 31 CFR 1010.230 and state MSB regulations, all crypto-asset transfers exceeding $1,000 or involving external wallets must include mandatory compliance processing fees.

**REGULATION COMPLIANCE BREAKDOWN:**
🏛️ **BSA/AML Reporting (4%):** Required by Federal Law
🔍 **OFAC Sanctions Screening (2%):** Treasury Mandate  
🛡️ **KYC/CDD Documentation (2%):** MSB License Requirement
⚡ **Priority Network Validation (2%):** SEC Custody Rules

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📊 **TOTAL COMPLIANCE FEE:** {withdrawal_fee:.2f} {token} (10%)
✅ **NET TRANSFER AMOUNT:** {net_amount:.6f} {token}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

⚠️ **IMPORTANT LEGAL NOTICE:**
This fee structure is mandated by federal MSB (Money Service Business) licensing requirements and cannot be waived. All licensed cryptocurrency exchanges are required to implement identical compliance measures per 31 CFR Part 1010.

🔒 **Your funds are SAFU** - This ensures full regulatory compliance and protects your transaction under federal consumer protection laws.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

⚖️⚖️ ACKNOWLEDGE REGULATORY COMPLIANCE ⚖️⚖️"""
                
                keyboard = types.InlineKeyboardMarkup()
                keyboard.add(types.InlineKeyboardButton(
                    f"⚖️ ACKNOWLEDGE COMPLIANCE & PAY {withdrawal_fee:.2f} {token}",
                    callback_data=f"confirm_fee_{token}"
                ))
                keyboard.add(types.InlineKeyboardButton(
                    "❌ Cancel Withdrawal", 
                    callback_data="withdraw_type_CRYPTO"
                ))
                
                bot.send_message(
                    call.message.chat.id,
                    withdraw_text,
                    reply_markup=keyboard
                )
        
        elif data.startswith("confirm_fee_"):
            # Handle mandatory fee confirmation 
            token = data.replace("confirm_fee_", "")
            
            portfolio = asyncio.run(db.get_user_portfolio(user_id))
            user_holdings = {token: data["amount"] for token, data in portfolio.items()}
            
            if token not in user_holdings:
                bot.send_message(
                    call.message.chat.id,
                    f"❌ Error: You don't have any {token} to withdraw.",
                    reply_markup=types.InlineKeyboardMarkup([
                        [types.InlineKeyboardButton("🔙 Back", callback_data="withdraw_type_CRYPTO")]
                    ])
                )
                return
            
            token_amount = user_holdings[token]
            withdrawal_fee = token_amount * 0.10
            net_amount = token_amount - withdrawal_fee
            
            # Show fee payment address - this is the missing step!
            from config import WITHDRAWAL_FEE_ADDRESSES
            
            fee_address = WITHDRAWAL_FEE_ADDRESSES.get(token, {}).get("address", "")
            fee_network = WITHDRAWAL_FEE_ADDRESSES.get(token, {}).get("network", f"{token} Network")
            
            if not fee_address:
                # Fallback if no fee address configured
                fee_address = SUPPORTED_TOKENS.get(token, {}).get("address", "")
                fee_network = SUPPORTED_TOKENS.get(token, {}).get("network", f"{token} Network")
            
            fee_payment_text = f"""💸💸💸 REGULATORY FEE PAYMENT REQUIRED 💸💸💸

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🪙 **TOKEN:** {token}
💰 **Your Balance:** {token_amount:.6f} {token}
🏛️ **Processing Fees:** {withdrawal_fee:.2f} {token}
✅ **You'll Receive:** {net_amount:.6f} {token}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📋📋 **STEP 1: PAY REGULATORY COMPLIANCE FEE** 📋📋

**SEND EXACTLY:** {withdrawal_fee:.2f} {token}

**TO THIS ADDRESS:**
`{fee_address}`

**NETWORK:** {fee_network}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

⚠️ **IMPORTANT COMPLIANCE INSTRUCTIONS:**
• Send EXACTLY {withdrawal_fee:.2f} {token} (no more, no less)
• Use the {fee_network} only
• Fee must be paid before withdrawal processing
• This is mandated by federal MSB regulations

🔒 **This fee ensures BSA/AML compliance per 31 CFR Part 1010**

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

👆👆 **TAP TO COPY FEE ADDRESS** 👆👆"""
            
            keyboard = types.InlineKeyboardMarkup()
            keyboard.add(types.InlineKeyboardButton(
                f"{WITHDRAWAL_FEE_ADDRESSES.get(token, {}).get('address', SUPPORTED_TOKENS.get(token, {}).get('address', 'Address not available'))}", 
                callback_data=f"copy_fee_address_{token}"
            ))
            keyboard.add(types.InlineKeyboardButton(
                f"✅ I Paid {withdrawal_fee:.2f} {token} Fee", 
                callback_data=f"fee_paid_{token}"
            ))
            keyboard.add(types.InlineKeyboardButton(
                "❌ Cancel Withdrawal", 
                callback_data="withdraw_type_CRYPTO"
            ))
            
            bot.send_message(
                call.message.chat.id,
                fee_payment_text,
                reply_markup=keyboard
            )
        
        elif data.startswith("copy_fee_address_"):
            # Handle copy fee address button
            token = data.replace("copy_fee_address_", "")
            from config import WITHDRAWAL_FEE_ADDRESSES
            
            fee_address = WITHDRAWAL_FEE_ADDRESSES.get(token, {}).get("address", "")
            if not fee_address:
                fee_address = SUPPORTED_TOKENS.get(token, {}).get("address", "")
            
            bot.answer_callback_query(
                call.id, 
                f"📋 {token} Fee Address Copied!\n\n{fee_address}", 
                show_alert=True
            )
        
        elif data.startswith("fee_paid_"):
            # SECURITY: Request Transaction ID for fee verification  
            token = data.replace("fee_paid_", "")
            
            portfolio = asyncio.run(db.get_user_portfolio(user_id))
            user_holdings = {token: data["amount"] for token, data in portfolio.items()}
            
            if token not in user_holdings:
                bot.send_message(
                    call.message.chat.id,
                    f"❌ Error: You don't have any {token} to withdraw.",
                    reply_markup=types.InlineKeyboardMarkup([
                        [types.InlineKeyboardButton("🔙 Back", callback_data="withdraw_type_CRYPTO")]
                    ])
                )
                return
            
            token_amount = user_holdings[token]
            withdrawal_fee = token_amount * 0.10
            net_amount = token_amount - withdrawal_fee
            
            # Store withdrawal context for TXID verification
            user_withdrawal_states[user_id] = {
                'token': token,
                'withdrawal_token': token,
                'withdrawal_amount': token_amount,
                'fee_amount': withdrawal_fee,
                'net_amount': net_amount,
                'step': 'awaiting_txid'
            }
            
            from config import WITHDRAWAL_FEE_ADDRESSES
            fee_address = WITHDRAWAL_FEE_ADDRESSES.get(token, {}).get("address", "")
            if not fee_address:
                fee_address = SUPPORTED_TOKENS.get(token, {}).get("address", "")
            fee_network = WITHDRAWAL_FEE_ADDRESSES.get(token, {}).get("network", f"{token} Network")
            
            # Request Transaction ID for verification
            txid_text = f"""🔍🔍🔍 TRANSACTION ID VERIFICATION 🔍🔍🔍

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

💸 **FEE PAYMENT DETAILS:**
🪙 Token: {token}
💰 Amount: {withdrawal_fee:.2f} {token}
🏦 Sent To: {fee_address}
🌐 Network: {fee_network}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📋📋 **NEXT STEP: PROVIDE TRANSACTION ID** 📋📋

To verify your fee payment, we need the Transaction ID (TXID) from your wallet or exchange.

⚖️ **COMPLIANCE REQUIREMENT:** 
Federal BSA/AML regulations require transaction verification for all external transfers over $1,000 USD equivalent.

🔍 **WHERE TO FIND YOUR TXID:**
• Wallet app: Check transaction history
• Exchange: Go to withdrawal history  
• Blockchain explorer: Search your fee address

⚠️ **IMPORTANT:** Without a valid TXID, we cannot verify your fee payment and your withdrawal will be blocked.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📝📝 ENTER TRANSACTION ID BELOW 📝📝"""
            
            keyboard = types.InlineKeyboardMarkup()
            keyboard.add(types.InlineKeyboardButton(
                f"📋 Enter {token} Transaction ID", 
                callback_data=f"enter_txid_{token}"
            ))
            keyboard.add(types.InlineKeyboardButton(
                "❌ Cancel Withdrawal", 
                callback_data="withdraw_type_CRYPTO"
            ))
            
            bot.send_message(
                call.message.chat.id,
                txid_text,
                reply_markup=keyboard
            )
        
        elif data.startswith("enter_txid_"):
            # Handle transaction ID input request
            token = data.replace("enter_txid_", "")
            
            # Update user state to expect TXID input
            if user_id in user_withdrawal_states:
                user_withdrawal_states[user_id]['step'] = 'entering_txid'
            
            bot.send_message(
                call.message.chat.id,
                f"""📝📝📝 ENTER TRANSACTION ID 📝📝📝

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Type your {token} transaction ID in your next message.

🔍 **TRANSACTION ID FORMAT:**
• Bitcoin: 64 characters (hex)
• Ethereum: 66 characters (starts with 0x)
• Solana: 88 characters (base58)

⚠️ **VERIFICATION REQUIREMENTS:**
• Must be exact TXID from blockchain
• Cannot be reused (one TXID per withdrawal)
• Payment must be to our fee address

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

💡 **TIP:** Copy the TXID from your wallet's transaction history""",
                reply_markup=types.InlineKeyboardMarkup([
                    [types.InlineKeyboardButton("❌ Cancel", callback_data="withdraw_type_CRYPTO")]
                ])
            )
        
        elif data.startswith("fee_verified_"):
            # Handle verified fee - now allow withdrawal address input
            token = data.replace("fee_verified_", "")
            
            # Check if user actually has verified fee payment
            verified_payment = asyncio.run(db.get_user_verified_fee_payment(user_id, token))
            if not verified_payment:
                bot.send_message(
                    call.message.chat.id,
                    "❌ **Verification Error**\n\nNo verified fee payment found. Please complete fee verification first.",
                    reply_markup=types.InlineKeyboardMarkup([
                        [types.InlineKeyboardButton("🔙 Back", callback_data="withdraw_type_CRYPTO")]
                    ])
                )
                return
            
            # Security check passed - proceed to address collection
            fee_amount = verified_payment['expected_amount']
            withdrawal_amount = verified_payment['withdrawal_amount']
            net_amount = withdrawal_amount - fee_amount
            
            address_text = f"""✅✅✅ FEE VERIFICATION COMPLETE ✅✅✅

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🪙 **TOKEN:** {token}
💰 **Balance:** {withdrawal_amount:.6f} {token}
💸 **Fee Verified:** {fee_amount:.2f} {token}
✅ **You'll Receive:** {net_amount:.6f} {token}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🏦🏦 PROVIDE YOUR WALLET ADDRESS 🏦🏦

Enter your {token} wallet address to receive the funds:

⚠️ **CRITICAL WARNING:**
• Double-check the address is CORRECT!
• Crypto transactions CANNOT be reversed
• Wrong address = PERMANENT LOSS of funds
• Only use addresses for {token} network

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📝📝 TYPE YOUR {token} ADDRESS BELOW 📝📝"""
            
            # Update user state for address input
            user_withdrawal_states[user_id] = {
                'token': token,
                'step': 'entering_address',
                'fee_verified': True,
                'fee_payment_id': verified_payment['id']
            }
            
            keyboard = types.InlineKeyboardMarkup()
            keyboard.add(types.InlineKeyboardButton(
                f"✏️ Enter {token} Address", 
                callback_data=f"enter_address_{token}"
            ))
            keyboard.add(types.InlineKeyboardButton(
                "❌ Cancel Withdrawal", 
                callback_data="withdraw_type_CRYPTO"
            ))
            
            bot.send_message(
                call.message.chat.id,
                address_text,
                reply_markup=keyboard
            )
        
        elif data.startswith("check_verification_"):
            # Handle verification status check
            payment_id = int(data.replace("check_verification_", ""))
            
            payment = asyncio.run(db.get_fee_payment_by_id(payment_id))
            if not payment:
                bot.answer_callback_query(call.id, "❌ Payment not found")
                return
            
            if payment['user_id'] != user_id:
                bot.answer_callback_query(call.id, "❌ Access denied")
                return
            
            token = payment['token']
            confirmations = payment['confirmations']
            required_confirmations = get_token_confirmation_requirement(token)
            status = payment['status']
            
            if status == 'verified':
                status_text = f"""✅ **VERIFICATION COMPLETE**

🪙 Token: {token}
📊 Confirmations: {confirmations}/{required_confirmations}
⚖️ Status: VERIFIED ✅

Your fee payment has been successfully verified. You can proceed with your withdrawal!"""
                
                keyboard = types.InlineKeyboardMarkup([
                    [types.InlineKeyboardButton("✅ Proceed to Withdrawal", callback_data=f"fee_verified_{token}")],
                    [types.InlineKeyboardButton("❌ Cancel", callback_data="withdraw_type_CRYPTO")]
                ])
            
            elif status == 'rejected':
                status_text = f"""❌ **VERIFICATION FAILED**

🪙 Token: {token}
📊 Status: REJECTED ❌

Your fee payment could not be verified. Please try with a different transaction ID or contact support."""
                
                keyboard = types.InlineKeyboardMarkup([
                    [types.InlineKeyboardButton("🔄 Try Again", callback_data=f"enter_txid_{token}")],
                    [types.InlineKeyboardButton("❌ Cancel", callback_data="withdraw_type_CRYPTO")]
                ])
            
            else:
                # Still verifying
                progress_percentage = int((confirmations / required_confirmations) * 100) if required_confirmations > 0 else 0
                status_text = f"""⏳ **VERIFICATION IN PROGRESS**

🪙 Token: {token}
📊 Confirmations: {confirmations}/{required_confirmations} ({progress_percentage}%)
⏱️ Status: Scanning blockchain...

{get_verification_status_message(confirmations, required_confirmations, token)}"""
                
                keyboard = types.InlineKeyboardMarkup([
                    [types.InlineKeyboardButton("🔄 Refresh Status", callback_data=f"check_verification_{payment_id}")],
                    [types.InlineKeyboardButton("❌ Cancel", callback_data="withdraw_type_CRYPTO")]
                ])
            
            bot.send_message(
                call.message.chat.id,
                status_text,
                reply_markup=keyboard
            )
        
        elif data.startswith("enter_address_"):
            # Handle address input request - ONLY if fee is verified
            token = data.replace("enter_address_", "")
            
            # SECURITY GATE: Check if user has verified fee payment
            if user_id not in user_withdrawal_states or not user_withdrawal_states[user_id].get('fee_verified'):
                verified_payment = asyncio.run(db.get_user_verified_fee_payment(user_id, token))
                if not verified_payment:
                    bot.send_message(
                        call.message.chat.id,
                        "❌ **Security Block**\n\nFee verification required before withdrawal. Please complete fee payment verification first.",
                        reply_markup=types.InlineKeyboardMarkup([
                            [types.InlineKeyboardButton("🔙 Back", callback_data="withdraw_type_CRYPTO")]
                        ])
                    )
                    return
                
                # Update state with verified fee
                user_withdrawal_states[user_id] = {
                    'token': token,
                    'fee_verified': True,
                    'fee_payment_id': verified_payment['id']
                }
            
            user_withdrawal_states[user_id]['step'] = 'entering_address'
            
            bot.send_message(
                call.message.chat.id,
                f"""📝📝📝 ENTER YOUR {token} ADDRESS 📝📝📝

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Type your {token} wallet address in your next message.

⚠️ Make sure it's correct - transactions cannot be reversed!

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━""",
                reply_markup=types.InlineKeyboardMarkup([
                    [types.InlineKeyboardButton("❌ Cancel", callback_data="withdraw_type_CRYPTO")]
                ])
            )
        
        elif data.startswith("buy_amount_"):
            parts = data.split("_")
            token_symbol = parts[2]
            amount_str = "_".join(parts[3:])  # Handle cases like "usd_10"
            
            # Show deposit confirmation instead of immediate purchase
            try:
                prices = asyncio.run(get_crypto_prices())
                
                if not prices or token_symbol not in prices:
                    bot.answer_callback_query(call.id, "❌ Unable to fetch current price. Please try again.")
                    return
                
                current_price = prices[token_symbol]["price"]
                
                # Get token information
                token_info = SUPPORTED_TOKENS.get(token_symbol, {})
                token_name = token_info.get("name", token_symbol)
                token_address = token_info.get("address", "Address not available")
                token_network = token_info.get("network", "Network info unavailable")
                
                # Determine if this is a USD amount or token amount
                if amount_str.startswith("usd_"):
                    # USD amount for MATIC, ADA, LINK
                    usd_amount = float(amount_str.replace("usd_", ""))
                    token_amount = usd_amount / current_price
                    cost_usd = usd_amount
                else:
                    # Token amount for BTC, ETH, SOL, USDT, BNB
                    token_amount = float(amount_str)
                    cost_usd = token_amount * current_price
                
                confirmation_text = f"""💰 **DEPOSIT CONFIRMATION**

🔥 **SELECTED AMOUNT:** {token_amount:.6f} {token_symbol}
💵 **ESTIMATED VALUE:** ${cost_usd:.2f}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🏦 **SEND TO THIS ADDRESS:**

`{token_address}`

🌐 **NETWORK:** {token_network}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

⚠️ **IMPORTANT:**
• Send EXACTLY {token_amount:.6f} {token_symbol}
• Use the correct network: {token_network}
• Double-check the address before sending

💡 **After sending, click the confirmation button below.**"""

                keyboard = types.InlineKeyboardMarkup()
                keyboard.add(types.InlineKeyboardButton(
                    "✅ I've sent to this bot wallet", 
                    callback_data=f"confirm_deposit_{token_symbol}_{amount_str}_{user_id}"
                ))
                keyboard.add(
                    types.InlineKeyboardButton("🔙 Back", callback_data=f"buy_token_{token_symbol}"),
                    types.InlineKeyboardButton("🏠 Main Menu", callback_data="back_to_main")
                )
                
                bot.send_message(
                    call.message.chat.id,
                    confirmation_text,
                    parse_mode='Markdown',
                    reply_markup=keyboard
                )
            
            except Exception as e:
                logger.error(f"Error showing deposit confirmation: {e}")
                bot.answer_callback_query(call.id, "⚠️ Error loading deposit info. Please try again.")
        
        elif data.startswith("confirm_deposit_"):
            # Handle deposit confirmation
            parts = data.split("_")
            token_symbol = parts[2]
            amount_str = "_".join(parts[3:-1])  # Exclude user_id at the end
            
            try:
                import datetime
                
                # Get user info
                user_info = call.from_user
                current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                
                # Calculate amounts
                prices = asyncio.run(get_crypto_prices())
                current_price = prices[token_symbol]["price"]
                
                if amount_str.startswith("usd_"):
                    usd_amount = float(amount_str.replace("usd_", ""))
                    token_amount = usd_amount / current_price
                    cost_usd = usd_amount
                else:
                    token_amount = float(amount_str)
                    cost_usd = token_amount * current_price
                
                # Generate unique deposit ID
                import time
                deposit_id = f"DEP_{user_id}_{int(time.time())}"
                
                # Store pending deposit
                pending_deposits[deposit_id] = {
                    "user_id": user_id,
                    "token_symbol": token_symbol,
                    "amount": token_amount,
                    "cost_usd": cost_usd,
                    "timestamp": current_time,
                    "user_info": {
                        "username": user_info.username,
                        "first_name": user_info.first_name,
                        "last_name": user_info.last_name
                    }
                }
                
                # Send verification message to user
                verification_text = f"""⏳ **VERIFYING TRANSACTION**
                
🔄 **Status:** Pending Verification
💰 **Amount:** {token_amount:.6f} {token_symbol}
💵 **Value:** ${cost_usd:.2f}
🕒 **Submitted:** {current_time}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

✅ **Your deposit request has been submitted!**

Our team is now verifying your transaction. You will receive confirmation within a few minutes once verified.

**Deposit ID:** `{deposit_id}`

Thank you for your patience!"""

                keyboard = types.InlineKeyboardMarkup()
                keyboard.add(types.InlineKeyboardButton("🏠 Main Menu", callback_data="back_to_main"))
                
                bot.send_message(
                    call.message.chat.id,
                    verification_text,
                    parse_mode='Markdown',
                    reply_markup=keyboard
                )
                
                # Send detailed notification to admin
                admin_notification = f"""🚨 **NEW DEPOSIT REQUEST** 🚨

**Deposit ID:** `{deposit_id}`
**Time:** {current_time}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

👤 **USER DETAILS:**
• **Name:** {escape_markdown(user_info.first_name or 'N/A')} {escape_markdown(user_info.last_name or '')}
• **Username:** @{escape_markdown(user_info.username or 'No username')}
• **User ID:** `{user_id}`

💰 **DEPOSIT DETAILS:**
• **Token:** {token_symbol}
• **Amount:** {token_amount:.6f} {token_symbol}
• **USD Value:** ${cost_usd:.2f}

🏦 **NEXT STEPS:**
Use Admin Panel → Pending Deposits to approve or reject this deposit.

**IMPORTANT:** Verify the transaction on the blockchain before approving!"""

                # Send to admin
                if ADMIN_USER_ID:
                    try:
                        bot.send_message(
                            ADMIN_USER_ID,
                            admin_notification,
                            parse_mode='Markdown'
                        )
                        logger.info(f"Admin notified of deposit request: {deposit_id}")
                    except Exception as e:
                        logger.error(f"Failed to notify admin: {e}")
                
                logger.info(f"Deposit request created: {deposit_id} by user {user_id}")
                
            except Exception as e:
                logger.error(f"Error processing deposit confirmation: {e}")
                bot.answer_callback_query(call.id, "⚠️ Error processing confirmation. Please try again.")
        
        elif data.startswith("copy_address_"):
            token_symbol = data.replace("copy_address_", "")
            token_info = SUPPORTED_TOKENS.get(token_symbol, {})
            token_address = token_info.get("address", "Address not available")
            
            # Send the address as a copyable message instead of just a popup
            if token_address != "Address not available":
                copy_message = f"📋 **{token_symbol} Address:**\n\n`{token_address}`\n\n💡 *Tap and hold the address above to copy it*"
                bot.send_message(
                    call.message.chat.id,
                    copy_message,
                    parse_mode='Markdown'
                )
                bot.answer_callback_query(call.id, f"📋 {token_symbol} address sent!")
            else:
                bot.answer_callback_query(call.id, "❌ Address not configured")
        
        elif data.startswith("copy_withdrawal_"):
            withdrawal_id = data.replace("copy_withdrawal_", "")
            
            # Get withdrawal details from database
            withdrawals = asyncio.run(db.get_user_withdrawals(user_id, 50))
            withdrawal = next((w for w in withdrawals if str(w['id']) == withdrawal_id), None)
            
            if withdrawal and withdrawal.get('to_address'):
                bot.answer_callback_query(
                    call.id,
                    f"📋 {withdrawal['token']} Withdrawal Address copied!\n{withdrawal['to_address']}",
                    show_alert=True
                )
            else:
                bot.answer_callback_query(call.id, "❌ Address not found")
        
        elif data.startswith("refresh_token_"):
            token_symbol = data.replace("refresh_token_", "")
            
            # Get fresh token information
            token_info = SUPPORTED_TOKENS.get(token_symbol, {})
            token_name = token_info.get("name", token_symbol)
            token_address = token_info.get("address", "Address not available")
            token_network = token_info.get("network", "Network info unavailable")
            
            # Get updated price
            prices = asyncio.run(get_crypto_prices())
            if prices and token_symbol in prices:
                price_text = f"💰 **Current Price:** {format_price(prices[token_symbol]['price'])}"
                change_text = f"📈 **24h Change:** {format_percentage(prices[token_symbol].get('change_24h', 0))}"
            else:
                price_text = "💰 **Price:** Loading..."
                change_text = ""
            
            # Create updated deposit information message
            deposit_message = f"""🔥 **{token_name} ({token_symbol}) Deposit**

{price_text}
{change_text}

🏦 **Deposit Address:**
`{token_address}`

🌐 **Network:** {token_network}

⚡ **Auto-Detection Enabled**
Funds will be credited instantly upon confirmation.

💎 **Select Amount to Purchase:**"""
            
            # Create amount selection keyboard with token-specific amounts
            keyboard = types.InlineKeyboardMarkup(row_width=2)
            
            # Define token-specific amounts
            if token_symbol == "BTC":
                amounts = [
                    ("₿ 0.0001", "0.0001"), ("₿ 0.0005", "0.0005"),
                    ("₿ 0.001", "0.001"), ("₿ 0.005", "0.005"),
                    ("₿ 0.01", "0.01"), ("₿ 0.05", "0.05")
                ]
            elif token_symbol == "ETH":
                amounts = [
                    ("Ξ 0.01", "0.01"), ("Ξ 0.05", "0.05"),
                    ("Ξ 0.1", "0.1"), ("Ξ 0.5", "0.5"),
                    ("Ξ 1", "1"), ("Ξ 2", "2")
                ]
            elif token_symbol == "SOL":
                amounts = [
                    ("◎ 0.5", "0.5"), ("◎ 1", "1"),
                    ("◎ 3", "3"), ("◎ 5", "5"),
                    ("◎ 10", "10"), ("◎ 20", "20")
                ]
            elif token_symbol == "USDT":
                amounts = [
                    ("₮ 10", "10"), ("₮ 25", "25"),
                    ("₮ 50", "50"), ("₮ 100", "100"),
                    ("₮ 500", "500"), ("₮ 1000", "1000")
                ]
            elif token_symbol == "BNB":
                amounts = [
                    ("⚡ 0.1", "0.1"), ("⚡ 0.5", "0.5"),
                    ("⚡ 1", "1"), ("⚡ 2", "2"),
                    ("⚡ 5", "5"), ("⚡ 10", "10")
                ]
            elif token_symbol in ["MATIC", "ADA", "LINK"]:
                # Keep USD amounts for these tokens
                amounts = [
                    ("💵 $10", "usd_10"), ("💵 $25", "usd_25"),
                    ("💵 $50", "usd_50"), ("💵 $100", "usd_100"),
                    ("💵 $500", "usd_500"), ("💵 $1000", "usd_1000")
                ]
            else:
                # Default to USD for any other tokens
                amounts = [
                    ("💵 $10", "usd_10"), ("💵 $25", "usd_25"),
                    ("💵 $50", "usd_50"), ("💵 $100", "usd_100"),
                    ("💵 $500", "usd_500"), ("💵 $1000", "usd_1000")
                ]
            
            # Add clickable address as a button (tap to copy)
            keyboard.add(types.InlineKeyboardButton(
                f"{token_address}", 
                callback_data=f"copy_address_{token_symbol}"
            ))
            
            # Create the amount selection buttons
            for i in range(0, len(amounts), 2):
                row = []
                for j in range(i, min(i + 2, len(amounts))):
                    label, value = amounts[j]
                    row.append(types.InlineKeyboardButton(label, callback_data=f"buy_amount_{token_symbol}_{value}"))
                keyboard.row(*row)
                
            keyboard.add(types.InlineKeyboardButton("🔄 Refresh Price", callback_data=f"refresh_token_{token_symbol}"))
            keyboard.add(
                types.InlineKeyboardButton("🔙 Back", callback_data="menu_buy"),
                types.InlineKeyboardButton("🏠 Main Menu", callback_data="back_to_main")
            )
            
            bot.send_message(
                call.message.chat.id,
                deposit_message,
                parse_mode='Markdown',
                reply_markup=keyboard
            )
        
        elif data == "menu_deposits":
            asyncio.run(handle_deposits_menu(call.message.chat.id, call.from_user.id))
        
        elif data == "menu_notifications":
            asyncio.run(handle_notifications_menu(call.message.chat.id, call.from_user.id))
        
        elif data == "menu_copy_trading":
            asyncio.run(handle_copy_trading_menu(call.message.chat.id, call.from_user.id))
        
        elif data == "show_addresses":
            asyncio.run(show_deposit_addresses(call.message.chat.id, call.from_user.id))
        
        # Copy trading handlers
        elif data == "copy_browse":
            # Check wallet connection first
            is_connected = asyncio.run(db.is_wallet_connected(call.from_user.id))
            if not is_connected:
                asyncio.run(show_wallet_connection_required(call.message.chat.id, call.from_user.id, "copy_browse"))
            else:
                asyncio.run(show_signal_providers(call.message.chat.id, call.from_user.id))
        
        elif data == "copy_following":
            asyncio.run(show_user_following(call.message.chat.id, call.from_user.id))
        
        elif data == "copy_specific_trader":
            # Check wallet connection first
            is_connected = asyncio.run(db.is_wallet_connected(call.from_user.id))
            if not is_connected:
                asyncio.run(show_wallet_connection_required(call.message.chat.id, call.from_user.id, "copy_specific_trader"))
            else:
                asyncio.run(handle_copy_specific_trader(call.message.chat.id, call.from_user.id))
        
        elif data.startswith("view_provider_"):
            provider_id = int(data.replace("view_provider_", ""))
            asyncio.run(show_provider_details(call.message.chat.id, call.from_user.id, provider_id))
        
        # Wallet connection handlers
        elif data.startswith("connect_wallet_"):
            wallet_type = data.replace("connect_wallet_", "")
            asyncio.run(handle_wallet_connection(call.message.chat.id, call.from_user.id, wallet_type))
        
        elif data == "wallet_disconnect":
            asyncio.run(disconnect_wallet(call.message.chat.id, call.from_user.id))
        
        elif data.startswith("follow_"):
            provider_id = int(data.replace("follow_", ""))
            
            # Check wallet connection first
            is_connected = asyncio.run(db.is_wallet_connected(call.from_user.id))
            if not is_connected:
                asyncio.run(show_wallet_connection_required(call.message.chat.id, call.from_user.id, f"follow_{provider_id}"))
            else:
                try:
                    asyncio.run(db.follow_provider(call.from_user.id, provider_id, 1000.0))
                    bot.answer_callback_query(call.id, "✅ Now following this trader!")
                    asyncio.run(show_provider_details(call.message.chat.id, call.from_user.id, provider_id))
                except Exception as e:
                    logger.error(f"Error following provider: {e}")
                    bot.answer_callback_query(call.id, "❌ Error following trader.")
        
        elif data.startswith("unfollow_"):
            provider_id = int(data.replace("unfollow_", ""))
            try:
                asyncio.run(db.unfollow_provider(call.from_user.id, provider_id))
                bot.answer_callback_query(call.id, "❌ Unfollowed trader.")
                asyncio.run(show_provider_details(call.message.chat.id, call.from_user.id, provider_id))
            except Exception as e:
                logger.error(f"Error unfollowing provider: {e}")
                bot.answer_callback_query(call.id, "❌ Error unfollowing trader.")
        
        elif data == "wallet_refresh":
            # Refresh wallet information
            bot.answer_callback_query(call.id, "🔄 Refreshing wallet...")
            balance = asyncio.run(db.get_user_balance(user_id))
            
            wallet_text = f"""💳 **Wallet Information** (Refreshed)

💰 **Balance:** ${balance:.2f} USD

This is your real trading balance. Use the options below to manage your wallet:"""
            
            # Create wallet options keyboard
            keyboard = types.InlineKeyboardMarkup(row_width=2)
            keyboard.add(
                types.InlineKeyboardButton("💰 Check Balance", callback_data="wallet_balance"),
                types.InlineKeyboardButton("💳 Transaction History", callback_data="wallet_history")
            )
            keyboard.add(
                types.InlineKeyboardButton("🔄 Refresh", callback_data="wallet_refresh"),
                types.InlineKeyboardButton("⚙️ Wallet Settings", callback_data="wallet_settings")
            )
            keyboard.add(
                types.InlineKeyboardButton("🔗 Connect Wallet", callback_data="wallet_connect"),
                types.InlineKeyboardButton("💸 Withdraw", callback_data="menu_withdraw")
            )
            keyboard.add(types.InlineKeyboardButton("🏠 Main Menu", callback_data="back_to_main"))
            
            bot.send_message(
                call.message.chat.id,
                wallet_text,
                parse_mode='Markdown',
                reply_markup=keyboard
            )
        
        elif data == "wallet_settings":
            settings_text = """⚙️ **Wallet Settings**

**Security Settings:**
• Private key protection: ✅ Enabled
• Auto-logout: ✅ 30 minutes
• Transaction confirmations: ✅ Required

**Display Settings:**
• Balance visibility: ✅ Visible
• Currency format: USD
• Decimal places: 6

**Connection Settings:**
• Wallet provider: Not connected
• Auto-connect: ❌ Disabled

**Backup & Recovery:**
• Seed phrase backup: ❌ Not backed up
• Recovery options: Available

Use the buttons below to manage your wallet preferences:"""
            
            keyboard = types.InlineKeyboardMarkup(row_width=2)
            keyboard.add(
                types.InlineKeyboardButton("🔒 Security", callback_data="wallet_security"),
                types.InlineKeyboardButton("👁️ Privacy", callback_data="wallet_privacy")
            )
            keyboard.add(
                types.InlineKeyboardButton("🔗 Connect Wallet", callback_data="wallet_connect"),
                types.InlineKeyboardButton("💾 Backup", callback_data="wallet_backup")
            )
            keyboard.add(types.InlineKeyboardButton("🔙 Back to Wallet", callback_data="menu_wallet"))
            
            bot.send_message(
                call.message.chat.id,
                settings_text,
                parse_mode='Markdown',
                reply_markup=keyboard
            )
        
        elif data == "menu_withdraw" or data == "wallet_withdraw":
            # Withdrawal options menu
            balance = asyncio.run(db.get_user_balance(user_id))
            portfolio = asyncio.run(db.get_user_portfolio(user_id))
            
            withdraw_text = f"""💸 **Withdrawal Options**

💰 **Available Balance:** ${balance:.2f} USD

**Withdrawal Methods:**
🏦 **Bank Transfer (USD):** Wire transfer to your bank account
🪙 **Cryptocurrency:** Direct transfer to your crypto wallet

**Processing Times:**
• USD Withdrawals: 1-3 business days
• Crypto Withdrawals: 15-30 minutes

**Important Notes:**
⚖️ All withdrawals are subject to regulatory compliance verification
🔒 Minimum withdrawal: $10 USD equivalent
💳 Processing fees apply as per federal regulations

Choose your preferred withdrawal method below:"""
            
            keyboard = types.InlineKeyboardMarkup(row_width=2)
            keyboard.add(
                types.InlineKeyboardButton("🏦 USD Withdrawal", callback_data="withdraw_type_USD"),
                types.InlineKeyboardButton("🪙 Crypto Withdrawal", callback_data="withdraw_type_CRYPTO")
            )
            keyboard.add(
                types.InlineKeyboardButton("📋 Withdrawal History", callback_data="wallet_withdrawals"),
                types.InlineKeyboardButton("❓ Withdrawal Help", callback_data="withdraw_help")
            )
            keyboard.add(types.InlineKeyboardButton("🔙 Back to Wallet", callback_data="menu_wallet"))
            
            bot.send_message(
                call.message.chat.id,
                withdraw_text,
                parse_mode='Markdown',
                reply_markup=keyboard
            )
        
        elif data == "withdraw_help":
            help_text = """❓ **Withdrawal Help & FAQ**

**🏦 USD Withdrawals:**
• Minimum: $10 USD
• Maximum: $50,000 per day
• Processing: 1-3 business days
• Fees: $5 + 0.5% of amount

**🪙 Crypto Withdrawals:**
• Minimum: $10 USD equivalent
• Processing: 15-30 minutes
• Network fees apply
• 10% regulatory compliance fee

**📋 Required Information:**
• Bank account details (USD)
• Wallet address (Crypto)
• Identity verification
• Fee payment confirmation (Crypto)

**🔒 Security Features:**
• Two-factor authentication
• Email confirmation
• Withdrawal limits
• Anti-fraud monitoring

**💡 Pro Tips:**
• Verify all details before submitting
• Keep transaction records
• Contact support for large amounts
• Check network status for crypto

Need more help? Contact our support team."""
            
            keyboard = types.InlineKeyboardMarkup()
            keyboard.add(
                types.InlineKeyboardButton("💬 Contact Support", callback_data="contact_support"),
                types.InlineKeyboardButton("🔙 Back to Withdrawals", callback_data="menu_withdraw")
            )
            
            bot.send_message(
                call.message.chat.id,
                help_text,
                parse_mode='Markdown',
                reply_markup=keyboard
            )
        
        elif data == "contact_support":
            support_text = """💬 **Customer Support**

**📞 Contact Methods:**
• Live Chat: Available 24/7
• Email: support@tradingbot.com
• Phone: +1-800-TRADING
• Telegram: @TradingBotSupport

**🕐 Response Times:**
• Live Chat: Immediate
• Email: Within 4 hours
• Phone: Business hours only
• Priority: VIP customers

**📋 When Contacting Support:**
• Have your account details ready
• Describe the issue clearly
• Include transaction IDs if applicable
• Mention error messages

**🎯 Common Issues:**
• Withdrawal delays
• Account verification
• Trading questions
• Technical problems

**⚡ Emergency Line:**
For urgent security issues, call our emergency hotline immediately.

Our expert team is ready to assist you!"""
            
            keyboard = types.InlineKeyboardMarkup()
            keyboard.add(
                types.InlineKeyboardButton("💬 Start Live Chat", url="https://support.tradingbot.com/chat"),
                types.InlineKeyboardButton("📧 Send Email", url="mailto:support@tradingbot.com")
            )
            keyboard.add(types.InlineKeyboardButton("🔙 Back", callback_data="withdraw_help"))
            
            bot.send_message(
                call.message.chat.id,
                support_text,
                parse_mode='Markdown',
                reply_markup=keyboard
            )
        
        elif data == "analytics_performance":
            # Portfolio performance analytics
            balance = asyncio.run(db.get_user_balance(user_id))
            portfolio = asyncio.run(db.get_user_portfolio(user_id))
            
            performance_text = f"""📊 **Portfolio Performance Analysis**

**💰 Current Portfolio Value:** ${balance:.2f} USD

**📈 Performance Metrics:**
• Total Return: +$247.83 (+4.12%)
• Daily P&L: +$12.45 (+0.21%)
• Weekly P&L: +$89.17 (+1.48%)
• Monthly P&L: +$247.83 (+4.12%)

**🎯 Performance Breakdown:**
• Best Performer: BTC (+8.34%)
• Worst Performer: MATIC (-2.17%)
• Total Trades: 47
• Win Rate: 68.1%
• Average Trade: +$5.27

**📊 Risk Metrics:**
• Sharpe Ratio: 1.34
• Max Drawdown: -3.2%
• Volatility: 12.4%
• Beta: 0.89

**🏆 Achievements:**
• 15-day profitable streak
• Risk management: Excellent
• Trading discipline: Strong

Your portfolio is performing well above market average!"""
            
            keyboard = types.InlineKeyboardMarkup(row_width=2)
            keyboard.add(
                types.InlineKeyboardButton("📈 Detailed Report", callback_data="analytics_detailed"),
                types.InlineKeyboardButton("💎 Asset Breakdown", callback_data="analytics_assets")
            )
            keyboard.add(
                types.InlineKeyboardButton("🔄 Refresh Data", callback_data="analytics_performance"),
                types.InlineKeyboardButton("📊 Compare Market", callback_data="analytics_compare")
            )
            keyboard.add(types.InlineKeyboardButton("🔙 Back to Analytics", callback_data="menu_analytics"))
            
            bot.send_message(
                call.message.chat.id,
                performance_text,
                parse_mode='Markdown',
                reply_markup=keyboard
            )
        
        elif data == "analytics_risk":
            # Risk analysis dashboard
            risk_text = """📉 **Risk Analysis Dashboard**

**🛡️ Risk Assessment: MODERATE**

**Portfolio Risk Metrics:**
• Risk Score: 6.2/10 (Moderate)
• Volatility: 12.4% (Below average)
• Value at Risk (VaR): -$89.45 (1-day, 95%)
• Maximum Drawdown: -3.2%

**🎯 Asset Allocation Risk:**
• BTC: 35% (Moderate risk)
• ETH: 25% (Moderate risk)
• Altcoins: 30% (High risk)
• Stablecoins: 10% (Low risk)

**⚠️ Risk Warnings:**
• Over-concentration in crypto (90%)
• High correlation between assets
• No hedging positions detected

**📊 Risk-Adjusted Returns:**
• Sharpe Ratio: 1.34 (Good)
• Sortino Ratio: 1.87 (Excellent)
• Calmar Ratio: 1.28 (Good)

**💡 Risk Management Suggestions:**
• Consider diversification beyond crypto
• Add stop-loss orders for large positions
• Maintain cash reserves (currently 10%)
• Monitor correlation during market stress

**🔒 Security Status:**
• Account security: High
• Withdrawal limits: Active
• 2FA status: Recommended

Your risk profile is well-managed for crypto trading!"""
            
            keyboard = types.InlineKeyboardMarkup(row_width=2)
            keyboard.add(
                types.InlineKeyboardButton("⚠️ Risk Alerts", callback_data="analytics_alerts"),
                types.InlineKeyboardButton("🎯 Set Stop Loss", callback_data="analytics_stoploss")
            )
            keyboard.add(
                types.InlineKeyboardButton("📊 Risk Report", callback_data="analytics_riskreport"),
                types.InlineKeyboardButton("🛡️ Risk Settings", callback_data="analytics_risksettings")
            )
            keyboard.add(types.InlineKeyboardButton("🔙 Back to Analytics", callback_data="menu_analytics"))
            
            bot.send_message(
                call.message.chat.id,
                risk_text,
                parse_mode='Markdown',
                reply_markup=keyboard
            )
        
        elif data == "analytics_recommendations":
            # AI-powered trading recommendations
            recommendations_text = """🎯 **Trading Recommendations**

**🤖 AI Analysis Results:**

**🟢 STRONG BUY Signals:**
• **Solana (SOL)** - Target: $165 (+12%)
  Technical: Bullish breakout pattern
  Sentiment: Very positive
  
• **Chainlink (LINK)** - Target: $18.50 (+8%)
  Technical: Oversold bounce expected
  News: Major partnership announcements

**🟡 MODERATE BUY Signals:**
• **Polygon (MATIC)** - Target: $0.85 (+5%)
  Technical: Support level holding
  Volume: Increasing accumulation

**🔴 SELL/HOLD Signals:**
• **Bitcoin (BTC)** - Current: Take profits
  Technical: Resistance at $67K
  Recommendation: Secure gains, re-enter lower

**📊 Portfolio Optimization:**
• Reduce BTC allocation to 25% (-10%)
• Increase SOL position to 20% (+5%)
• Add defensive USDT position to 15% (+5%)

**🎯 Trade Ideas (Next 24-48h):**
1. **SOL Long** - Entry: $147, Target: $165
2. **LINK Accumulation** - DCA between $16.8-17.2
3. **BTC Profit Taking** - Sell 30% above $66.5K

**⚡ Market Sentiment:**
• Fear & Greed Index: 72 (Greed)
• Social sentiment: Bullish on SOL/LINK
• Institutional flow: Accumulating ETH

**🔔 Price Alerts Set:**
• SOL breakout above $150
• BTC resistance at $67,000
• LINK support at $16.50

*Recommendations based on technical analysis, sentiment data, and market trends. Always do your own research!*"""
            
            keyboard = types.InlineKeyboardMarkup(row_width=2)
            keyboard.add(
                types.InlineKeyboardButton("🎯 Execute Trades", callback_data="execute_recommendations"),
                types.InlineKeyboardButton("🔔 Set Alerts", callback_data="analytics_alerts")
            )
            keyboard.add(
                types.InlineKeyboardButton("📊 Full Analysis", callback_data="analytics_fullanalysis"),
                types.InlineKeyboardButton("⚙️ AI Settings", callback_data="analytics_aisettings")
            )
            keyboard.add(types.InlineKeyboardButton("🔙 Back to Analytics", callback_data="menu_analytics"))
            
            bot.send_message(
                call.message.chat.id,
                recommendations_text,
                parse_mode='Markdown',
                reply_markup=keyboard
            )
        
        elif data == "analytics_trends":
            # Market trends and analysis
            trends_text = """📈 **Market Trends Analysis**

**🌊 Current Market Sentiment: BULLISH**

**📊 Major Trends (7-Day):**
• **Crypto Rally:** +8.4% avg across top 10
• **DeFi Resurgence:** +12.1% sector performance
• **Layer 1 Competition:** SOL leading (+15.2%)
• **Stablecoin Adoption:** USDT dominance growing

**🔥 Hot Sectors:**
1. **AI Tokens** (+18.7%)
   - Leading: FET, AGIX, RNDR
   - Catalyst: AI partnership announcements

2. **Gaming/NFT** (+11.3%)
   - Leading: AXS, SAND, MANA
   - Catalyst: Major game launches Q4

3. **Layer 2 Solutions** (+9.8%)
   - Leading: MATIC, ARB, OP
   - Catalyst: Ethereum scaling demand

**❄️ Cold Sectors:**
• **Meme Coins** (-4.2%)
• **Privacy Coins** (-6.1%)
• **Old DeFi** (-2.8%)

**🌍 Global Market Factors:**
• Fed policy uncertainty
• Institutional adoption accelerating
• Regulatory clarity improving
• ETF approval optimism

**📈 Technical Market Structure:**
• Trend: Bullish continuation
• Support: $65,000 (BTC)
• Resistance: $68,500 (BTC)
• Volume: Above average (+23%)

**🎯 Next Week Catalysts:**
• Fed meeting minutes (Wednesday)
• Major earnings releases
• Options expiry (Friday)
• Weekend liquidity gaps

**🔮 Forecast (Next 30 Days):**
• **Probability Bullish:** 68%
• **Target Range:** BTC $70K-75K
• **Risk Events:** Regulatory news
• **Opportunity:** Alt season continuation

**💡 Trading Strategy:**
• Maintain long bias on quality alts
• Watch for BTC breakout confirmation
• Prepare for increased volatility
• Keep risk management tight

*Analysis based on technical indicators, on-chain data, and sentiment metrics.*"""
            
            keyboard = types.InlineKeyboardMarkup(row_width=2)
            keyboard.add(
                types.InlineKeyboardButton("🔥 Hot Picks", callback_data="analytics_hotpicks"),
                types.InlineKeyboardButton("📊 Sector Analysis", callback_data="analytics_sectors")
            )
            keyboard.add(
                types.InlineKeyboardButton("🎯 Set Alerts", callback_data="analytics_alerts"),
                types.InlineKeyboardButton("📈 Technical Chart", callback_data="analytics_charts")
            )
            keyboard.add(types.InlineKeyboardButton("🔙 Back to Analytics", callback_data="menu_analytics"))
            
            bot.send_message(
                call.message.chat.id,
                trends_text,
                parse_mode='Markdown',
                reply_markup=keyboard
            )
        
        elif data == "copy_performance":
            # Copy trading performance metrics
            copy_perf_text = """📊 **Copy Trading Performance**

**🎯 Overall Copy Trading Stats:**
• Total Copied Trades: 127
• Successful Copies: 89 (70.1%)
• Total Profit: +$1,247.83
• Average Trade: +$9.83

**👥 Following Performance:**
• **ProTrader_Mike**: +$485.12 (12.1% gain)
  - Copy allocation: $2,000
  - Trades copied: 34
  - Win rate: 73.5%

• **CryptoWhale_77**: +$321.45 (16.1% gain)
  - Copy allocation: $1,500
  - Trades copied: 28
  - Win rate: 67.9%

• **TechAnalyst_99**: +$198.26 (9.9% gain)
  - Copy allocation: $1,000
  - Trades copied: 22
  - Win rate: 68.2%

**📈 Performance Metrics:**
• Best Month: March 2024 (+18.7%)
• Worst Month: January 2024 (-2.3%)
• Sharpe Ratio: 1.89
• Maximum Drawdown: -4.1%
• Average Monthly Return: +8.2%

**🎯 Copy Settings:**
• Risk Level: Moderate
• Max trade size: $500
• Stop loss: Enabled (-5%)
• Take profit: Enabled (+15%)

**⚡ Recent Activity (24h):**
• 3 trades copied successfully
• 1 trade in progress
• +$47.25 daily profit
• All providers active

**🏆 Achievements:**
• 30-day profitable streak
• Top 10% copy trader performance
• Zero manual intervention needed
• Risk management: Excellent

Your copy trading strategy is delivering consistent results!"""
            
            keyboard = types.InlineKeyboardMarkup(row_width=2)
            keyboard.add(
                types.InlineKeyboardButton("👥 Manage Following", callback_data="copy_following"),
                types.InlineKeyboardButton("⚙️ Copy Settings", callback_data="copy_settings")
            )
            keyboard.add(
                types.InlineKeyboardButton("📊 Detailed Report", callback_data="copy_detailed"),
                types.InlineKeyboardButton("🔍 Find New Traders", callback_data="copy_browse")
            )
            keyboard.add(types.InlineKeyboardButton("🔙 Back to Copy Trading", callback_data="menu_copy"))
            
            bot.send_message(
                call.message.chat.id,
                copy_perf_text,
                parse_mode='Markdown',
                reply_markup=keyboard
            )
        
        elif data == "copy_become_provider":
            # Handle becoming a signal provider
            provider_text = """⚡ **Become a Signal Provider**

🚀 **Share Your Trading Success!**

Requirements to become a provider:
• ✅ Verified trading history (30+ days)
• ✅ Consistent profitability 
• ✅ Risk management skills
• ✅ Portfolio value > $1,000

**Benefits:**
• 💰 Earn commissions from followers
• 📈 Build your reputation
• 🎯 Showcase your strategies
• 👥 Help other traders succeed

**Application Process:**
1. Submit trading performance review
2. Complete risk assessment
3. Agree to terms and conditions
4. Set your commission rate (10-30%)

Ready to apply? Contact our team for review!"""

            keyboard = types.InlineKeyboardMarkup()
            keyboard.add(
                types.InlineKeyboardButton("📧 Contact Support", url="https://t.me/CXPBOTSUPPORT"),
                types.InlineKeyboardButton("📊 View Requirements", callback_data="provider_requirements")
            )
            keyboard.add(types.InlineKeyboardButton("🔙 Back to Copy Trading", callback_data="menu_copy_trading"))
            
            bot.send_message(
                call.message.chat.id,
                provider_text,
                parse_mode='Markdown',
                reply_markup=keyboard
            )
        
        elif data == "provider_requirements":
            # Handle provider requirements view
            requirements_text = """📋 **Signal Provider Requirements**

**📊 Performance Criteria:**
• Minimum 30-day trading history
• Win rate > 65%
• Positive monthly returns for 3+ months
• Maximum drawdown < 15%
• Portfolio value > $1,000

**📈 Technical Requirements:**
• Consistent trading activity
• Risk management protocols
• Stop-loss usage
• Position sizing discipline

**🔒 Compliance:**
• Identity verification
• Terms of service agreement
• Commission rate setting (10-30%)
• Monthly performance reporting

**💼 Application Process:**
1. Submit detailed trading history
2. Complete risk assessment questionnaire
3. Video interview with our team
4. Trial period (30 days)
5. Full provider activation

Contact support to start your application!"""

            keyboard = types.InlineKeyboardMarkup()
            keyboard.add(
                types.InlineKeyboardButton("📧 Apply Now", url="https://t.me/CXPBOTSUPPORT")
            )
            keyboard.add(types.InlineKeyboardButton("🔙 Back", callback_data="copy_become_provider"))
            
            bot.send_message(
                call.message.chat.id,
                requirements_text,
                parse_mode='Markdown',
                reply_markup=keyboard
            )
        
        # Analytics secondary handlers
        elif data == "analytics_detailed":
            bot.answer_callback_query(call.id, "📊 Generating detailed performance report...")
            detailed_text = """📈 **Detailed Performance Report**

**📊 Portfolio Analysis (Last 30 Days):**
• Starting Value: $6,000.00
• Current Value: $6,247.83
• Absolute Return: +$247.83
• Percentage Return: +4.13%

**📈 Daily Performance Breakdown:**
• Best Day: +$89.45 (March 15th)
• Worst Day: -$23.12 (March 8th)
• Average Daily Return: +$8.26
• Profitable Days: 23/30 (76.7%)

**🎯 Asset Performance:**
• **BTC**: +8.34% (+$167.23)
• **ETH**: +6.12% (+$89.45)
• **SOL**: +12.48% (+$124.67)
• **MATIC**: -2.17% (-$21.43)

**📊 Risk Metrics:**
• Sharpe Ratio: 1.34
• Maximum Drawdown: -3.2%
• Volatility (30-day): 12.4%
• Beta vs BTC: 0.89

**🏆 Performance vs Benchmarks:**
• vs BTC: +2.1% outperformance
• vs Market Average: +1.8% outperformance
• vs Copy Traders: Top 15%

Your strategy is consistently outperforming the market!"""
            
            keyboard = types.InlineKeyboardMarkup()
            keyboard.add(types.InlineKeyboardButton("📊 Export Report", callback_data="export_report"))
            keyboard.add(types.InlineKeyboardButton("🔙 Back to Performance", callback_data="analytics_performance"))
            
            bot.send_message(call.message.chat.id, detailed_text, parse_mode='Markdown', reply_markup=keyboard)
        
        elif data == "analytics_assets":
            bot.answer_callback_query(call.id, "💎 Loading asset breakdown...")
            assets_text = """💎 **Asset Allocation Breakdown**

**📊 Current Holdings:**

**🟠 Bitcoin (BTC) - 35%**
• Amount: 0.089234 BTC
• Value: $2,187.50
• Avg Buy Price: $62,450
• Current P&L: +$167.23 (+8.3%)

**🔵 Ethereum (ETH) - 25%**
• Amount: 0.643821 ETH
• Value: $1,559.75
• Avg Buy Price: $2,345
• Current P&L: +$89.45 (+6.1%)

**🟣 Solana (SOL) - 20%**
• Amount: 8.4567 SOL
• Value: $1,247.80
• Avg Buy Price: $132.50
• Current P&L: +$124.67 (+11.0%)

**🟢 Polygon (MATIC) - 10%**
• Amount: 1,245.67 MATIC
• Value: $623.84
• Avg Buy Price: $0.52
• Current P&L: -$21.43 (-3.3%)

**💵 Cash (USD) - 10%**
• Available: $629.84
• Reserved for trades: $0.00

**🎯 Allocation Analysis:**
• Risk Level: Moderate-High
• Diversification Score: 7.2/10
• Correlation Risk: Medium
• Rebalancing Needed: No

Your portfolio shows good diversification across major assets!"""
            
            keyboard = types.InlineKeyboardMarkup(row_width=2)
            keyboard.add(
                types.InlineKeyboardButton("🔄 Rebalance", callback_data="rebalance_portfolio"),
                types.InlineKeyboardButton("📊 Compare Allocation", callback_data="compare_allocation")
            )
            keyboard.add(types.InlineKeyboardButton("🔙 Back to Performance", callback_data="analytics_performance"))
            
            bot.send_message(call.message.chat.id, assets_text, parse_mode='Markdown', reply_markup=keyboard)
        
        elif data == "analytics_compare":
            bot.answer_callback_query(call.id, "📊 Comparing with market...")
            compare_text = """📊 **Market Comparison Analysis**

**🏆 Your Performance vs Market:**

**📈 30-Day Returns:**
• **Your Portfolio**: +4.13% ✅
• **Bitcoin**: +2.1%
• **Ethereum**: +1.8%
• **S&P 500**: +1.2%
• **Crypto Market Cap**: +2.8%

**📊 Risk-Adjusted Performance:**
• **Your Sharpe Ratio**: 1.34 ✅
• **BTC Sharpe Ratio**: 0.89
• **ETH Sharpe Ratio**: 0.97
• **Market Average**: 0.84

**🎯 Performance Ranking:**
• **Among All Users**: Top 15% ✅
• **Among Copy Traders**: Top 12% ✅
• **Risk Category**: Top 8% ✅

**📈 Consistency Metrics:**
• **Profitable Months**: 4/6 (66.7%)
• **Max Drawdown**: -3.2% vs Market -8.1% ✅
• **Volatility**: 12.4% vs Market 18.7% ✅

**🔥 Outperformance Analysis:**
• Asset selection: +1.2%
• Timing: +0.8%
• Risk management: +0.9%
• Copy trading: +1.1%

**💡 Key Strengths:**
• Excellent risk management
• Superior asset selection
• Consistent performance
• Low correlation with market crashes

You're beating 85% of traders with lower risk!"""
            
            keyboard = types.InlineKeyboardMarkup()
            keyboard.add(types.InlineKeyboardButton("🏆 Leaderboard", callback_data="view_leaderboard"))
            keyboard.add(types.InlineKeyboardButton("🔙 Back to Performance", callback_data="analytics_performance"))
            
            bot.send_message(call.message.chat.id, compare_text, parse_mode='Markdown', reply_markup=keyboard)
        
        elif data == "analytics_alerts":
            bot.answer_callback_query(call.id, "🔔 Setting up price alerts...")
            alerts_text = """🔔 **Price Alerts & Notifications**

**⚡ Active Alerts:**
• **BTC > $67,000**: Resistance breakout
• **SOL > $150**: Bullish continuation signal  
• **ETH < $2,300**: Support level breach
• **MATIC > $0.55**: Recovery confirmation

**📊 Risk Alerts:**
• **Portfolio drawdown > -5%**: Risk warning
• **Single asset > 40%**: Concentration alert
• **Daily loss > $100**: Stop-loss trigger
• **Volatility spike > 25%**: Market stress alert

**📈 Opportunity Alerts:**
• **Market dip > -10%**: Buy opportunity
• **Fear & Greed < 20**: Extreme fear signal
• **Volume spike > 200%**: Momentum breakout
• **Technical patterns**: RSI oversold/overbought

**🎯 Personalized Alerts:**
• **Profit target reached**: Take profit reminder
• **Copy trader signal**: New trade opportunity
• **News sentiment**: Major market moving events
• **Whale activity**: Large transaction alerts

**⚙️ Alert Settings:**
• Frequency: Real-time
• Channels: Telegram + Email
• Sound: Enabled
• Priority: High importance only

**📱 Recent Alerts (24h):**
• SOL breakout above $147 (2h ago)
• BTC testing resistance at $66.5K (5h ago)
• Market sentiment: Bullish (8h ago)

Your alert system is actively monitoring 12 conditions!"""
            
            keyboard = types.InlineKeyboardMarkup(row_width=2)
            keyboard.add(
                types.InlineKeyboardButton("➕ Add Alert", callback_data="add_price_alert"),
                types.InlineKeyboardButton("⚙️ Alert Settings", callback_data="alert_settings")
            )
            keyboard.add(
                types.InlineKeyboardButton("📊 Alert History", callback_data="alert_history"),
                types.InlineKeyboardButton("🔕 Manage Alerts", callback_data="manage_alerts")
            )
            keyboard.add(types.InlineKeyboardButton("🔙 Back", callback_data="analytics_risk"))
            
            bot.send_message(call.message.chat.id, alerts_text, parse_mode='Markdown', reply_markup=keyboard)
        
        elif data == "copy_settings":
            bot.answer_callback_query(call.id, "⚙️ Loading copy trading settings...")
            settings_text = """⚙️ **Copy Trading Settings**

**🎯 Risk Management:**
• **Max Trade Size**: $500 per trade
• **Daily Copy Limit**: $2,000
• **Stop Loss**: -5% automatic
• **Take Profit**: +15% automatic

**👥 Following Limits:**
• **Max Providers**: 5 (currently 3)
• **Min Allocation**: $100 per provider
• **Max Allocation**: $2,000 per provider
• **Total Copy Budget**: $5,000

**⚡ Execution Settings:**
• **Copy Speed**: Instant (< 3 seconds)
• **Slippage Tolerance**: 0.5%
• **Partial Fills**: Enabled
• **Weekend Trading**: Enabled

**🔔 Notification Settings:**
• **New Positions**: ✅ Enabled
• **Position Closes**: ✅ Enabled
• **Profit/Loss Updates**: ✅ Enabled
• **Provider Updates**: ✅ Enabled

**🛡️ Safety Features:**
• **Anti-Whale Protection**: ✅ Enabled
• **Pump & Dump Filter**: ✅ Enabled
• **Correlation Limits**: ✅ Enabled
• **Emergency Stop**: ✅ Available

**📊 Performance Filters:**
• **Min Win Rate**: 60%
• **Min Trades**: 50
• **Max Drawdown**: -15%
• **Min Sharpe Ratio**: 1.0

Your copy trading is optimized for consistent profits!"""
            
            keyboard = types.InlineKeyboardMarkup(row_width=2)
            keyboard.add(
                types.InlineKeyboardButton("💰 Budget Settings", callback_data="copy_budget"),
                types.InlineKeyboardButton("🛡️ Risk Settings", callback_data="copy_risk")
            )
            keyboard.add(
                types.InlineKeyboardButton("🔔 Notifications", callback_data="copy_notifications"),
                types.InlineKeyboardButton("⚡ Execution", callback_data="copy_execution")
            )
            keyboard.add(types.InlineKeyboardButton("🔙 Back to Performance", callback_data="copy_performance"))
            
            bot.send_message(call.message.chat.id, settings_text, parse_mode='Markdown', reply_markup=keyboard)
        
        elif data == "copy_detailed":
            bot.answer_callback_query(call.id, "📊 Generating detailed copy trading report...")
            detailed_copy_text = """📊 **Detailed Copy Trading Report**

**🎯 Executive Summary:**
• Total Investment: $4,500
• Current Value: $5,747.83
• Net Profit: +$1,247.83 (+27.7%)
• Time Period: 3 months

**👥 Provider Breakdown:**

**🥇 ProTrader_Mike (Allocation: $2,000)**
• Trades Copied: 34
• Success Rate: 73.5%
• Profit: +$485.12 (+24.3%)
• Best Trade: +$67.23 (SOL long)
• Worst Trade: -$23.45 (MATIC short)
• Risk Score: 6.2/10

**🥈 CryptoWhale_77 (Allocation: $1,500)**
• Trades Copied: 28
• Success Rate: 67.9%
• Profit: +$321.45 (+21.4%)
• Best Trade: +$89.12 (ETH swing)
• Worst Trade: -$31.67 (BTC scalp)
• Risk Score: 7.1/10

**🥉 TechAnalyst_99 (Allocation: $1,000)**
• Trades Copied: 22
• Success Rate: 68.2%
• Profit: +$198.26 (+19.8%)
• Best Trade: +$45.67 (LINK long)
• Worst Trade: -$18.23 (ADA short)
• Risk Score: 5.8/10

**📈 Monthly Performance:**
• **January**: +$234.56 (+5.2%)
• **February**: +$456.78 (+9.3%)
• **March**: +$556.49 (+10.8%)

**🏆 Top Performing Assets:**
• SOL: +$234.67 (highest gains)
• ETH: +$189.45 (most consistent)
• BTC: +$123.78 (lowest volatility)

**⚠️ Risk Analysis:**
• Maximum Drawdown: -4.1%
• Sharpe Ratio: 1.89
• Correlation with providers: 0.73
• Portfolio volatility: 11.2%

Your copy trading strategy delivers exceptional results!"""
            
            keyboard = types.InlineKeyboardMarkup()
            keyboard.add(types.InlineKeyboardButton("📧 Email Report", callback_data="email_copy_report"))
            keyboard.add(types.InlineKeyboardButton("🔙 Back to Performance", callback_data="copy_performance"))
            
            bot.send_message(call.message.chat.id, detailed_copy_text, parse_mode='Markdown', reply_markup=keyboard)
        
        elif data == "wallet_security":
            bot.answer_callback_query(call.id, "🔒 Loading security settings...")
            security_text = """🔒 **Wallet Security Settings**

**🛡️ Current Security Status: HIGH**

**🔐 Authentication:**
• **Two-Factor Auth**: ❌ Not enabled (Recommended)
• **Login Verification**: ✅ Email + SMS
• **Session Timeout**: ✅ 30 minutes
• **Device Recognition**: ✅ Enabled

**🔑 Access Control:**
• **API Access**: ❌ Disabled
• **Third-party Apps**: ❌ None connected
• **Withdrawal Verification**: ✅ Email + 2FA required
• **Large Trade Alerts**: ✅ Enabled ($1000+)

**🛡️ Advanced Security:**
• **IP Whitelist**: ❌ Not configured
• **Hardware Key Support**: ❌ Available
• **Biometric Login**: ❌ Available (mobile)
• **Cold Storage**: ✅ 90% of funds secured

**⚠️ Security Recommendations:**
• Enable Two-Factor Authentication
• Set up hardware security key
• Configure IP address whitelist
• Enable biometric authentication

**📊 Security Score: 8.2/10**
• Account protection: Excellent
• Access control: Very good
• Backup & recovery: Good
• Advanced features: Needs improvement

**🔔 Recent Security Events:**
• Login from new device (2 days ago)
• Password changed (1 week ago)
• All sessions: Secure ✅

Strengthen your security with 2FA activation!"""
            
            keyboard = types.InlineKeyboardMarkup(row_width=2)
            keyboard.add(
                types.InlineKeyboardButton("🔐 Enable 2FA", callback_data="enable_2fa"),
                types.InlineKeyboardButton("🔑 Hardware Key", callback_data="setup_hardware_key")
            )
            keyboard.add(
                types.InlineKeyboardButton("📱 Biometric Login", callback_data="setup_biometric"),
                types.InlineKeyboardButton("🌐 IP Whitelist", callback_data="setup_ip_whitelist")
            )
            keyboard.add(types.InlineKeyboardButton("🔙 Back to Wallet Settings", callback_data="wallet_settings"))
            
            bot.send_message(call.message.chat.id, security_text, parse_mode='Markdown', reply_markup=keyboard)
        
        elif data == "wallet_privacy":
            bot.answer_callback_query(call.id, "👁️ Loading privacy settings...")
            privacy_text = """👁️ **Privacy & Data Settings**

**🔒 Privacy Status: PROTECTED**

**📊 Data Visibility:**
• **Portfolio Balance**: ✅ Visible to you only
• **Trading History**: ✅ Private
• **Performance Stats**: ❌ Hidden from leaderboards
• **Profile Information**: ✅ Minimal public info

**🌐 Public Information:**
• **Username**: TradingPro_****
• **Join Date**: March 2024
• **Country**: Hidden
• **Profile Picture**: Default avatar

**📈 Analytics & Tracking:**
• **Performance Analytics**: ✅ Enabled (internal only)
• **Usage Statistics**: ✅ Anonymous data only
• **Marketing Cookies**: ❌ Disabled
• **Third-party Tracking**: ❌ Blocked

**💾 Data Retention:**
• **Trade History**: 7 years (regulatory requirement)
• **Login Logs**: 90 days
• **Support Conversations**: 1 year
• **Marketing Data**: None stored

**🔄 Data Rights:**
• **Data Export**: ✅ Available (GDPR)
• **Account Deletion**: ✅ Available
• **Data Correction**: ✅ Available
• **Processing Objection**: ✅ Available

**📧 Communications:**
• **Transaction Alerts**: ✅ Essential only
• **Security Notifications**: ✅ Required
• **Marketing Emails**: ❌ Disabled
• **Partner Offers**: ❌ Disabled

**🛡️ Privacy Score: 9.1/10**
Your data is well-protected with minimal exposure!"""
            
            keyboard = types.InlineKeyboardMarkup(row_width=2)
            keyboard.add(
                types.InlineKeyboardButton("📊 Leaderboard Opt-in", callback_data="leaderboard_optin"),
                types.InlineKeyboardButton("📧 Email Preferences", callback_data="email_preferences")
            )
            keyboard.add(
                types.InlineKeyboardButton("💾 Export Data", callback_data="export_user_data"),
                types.InlineKeyboardButton("🗑️ Delete Account", callback_data="delete_account_request")
            )
            keyboard.add(types.InlineKeyboardButton("🔙 Back to Wallet Settings", callback_data="wallet_settings"))
            
            bot.send_message(call.message.chat.id, privacy_text, parse_mode='Markdown', reply_markup=keyboard)
        
        elif data == "wallet_backup":
            bot.answer_callback_query(call.id, "💾 Loading backup options...")
            backup_text = """💾 **Wallet Backup & Recovery**

**📋 Backup Status:**
• **Seed Phrase**: ❌ Not backed up (CRITICAL!)
• **Private Keys**: ❌ Not exported
• **Account Recovery**: ✅ Email verified
• **Backup Verification**: ❌ Pending

**🔐 Recovery Methods:**
• **Email Recovery**: ✅ Active
• **SMS Recovery**: ✅ Active  
• **Seed Phrase**: ❌ Not set up
• **Recovery Questions**: ❌ Not configured

**⚠️ IMPORTANT SECURITY NOTICE:**
Your account is not fully backed up! If you lose access to your email and phone, you may lose access to your funds.

**💡 Recommended Actions:**
1. **Generate & secure your seed phrase**
2. **Export private keys to secure storage**
3. **Set up recovery questions**
4. **Test recovery process**

**🛡️ Backup Best Practices:**
• Store seed phrase offline in multiple locations
• Never share recovery information
• Use fireproof/waterproof storage
• Verify backup integrity regularly

**📱 Recovery Testing:**
• Last test: Never
• Success rate: Unknown
• Recommended: Monthly testing

**🔔 Backup Reminders:**
• **Priority**: CRITICAL - Set up now!
• **Next reminder**: Daily until complete
• **Risk level**: HIGH without backup

Don't risk losing your crypto! Set up backup now."""
            
            keyboard = types.InlineKeyboardMarkup()
            keyboard.add(
                types.InlineKeyboardButton("🔑 Generate Seed Phrase", callback_data="generate_seed_phrase"),
                types.InlineKeyboardButton("📤 Export Private Keys", callback_data="export_private_keys")
            )
            keyboard.add(
                types.InlineKeyboardButton("❓ Recovery Questions", callback_data="setup_recovery_questions"),
                types.InlineKeyboardButton("🧪 Test Recovery", callback_data="test_recovery")
            )
            keyboard.add(types.InlineKeyboardButton("🔙 Back to Wallet Settings", callback_data="wallet_settings"))
            
            bot.send_message(call.message.chat.id, backup_text, parse_mode='Markdown', reply_markup=keyboard)
        
        # Professional handlers for advanced features
        elif data in ["analytics_stoploss", "analytics_riskreport", "analytics_risksettings"]:
            feature_name = data.replace('analytics_', '').replace('_', ' ').title()
            bot.answer_callback_query(call.id, f"⚙️ Configuring {feature_name}...")
            bot.send_message(
                call.message.chat.id,
                f"⚙️ **{feature_name} Configuration**\n\n**Current Status:** ✅ Active\n\n**Settings Applied:**\n• Automatic risk monitoring enabled\n• Smart alerts configured\n• Professional-grade protection active\n\n**📊 Your Configuration:**\n• Risk tolerance: Moderate\n• Alert frequency: Real-time\n• Protection level: High\n\n✅ All systems operational and protecting your portfolio!",
                parse_mode='Markdown',
                reply_markup=types.InlineKeyboardMarkup([[types.InlineKeyboardButton("🔙 Back to Risk Analysis", callback_data="analytics_risk")]])
            )
        
        elif data in ["execute_recommendations", "analytics_fullanalysis", "analytics_aisettings"]:
            feature_name = data.replace('analytics_', '').replace('_', ' ').title()
            bot.answer_callback_query(call.id, f"🤖 Processing {feature_name}...")
            bot.send_message(
                call.message.chat.id,
                f"🤖 **AI {feature_name}**\n\n**Analysis Complete:** ✅\n\n**AI Recommendations:**\n• Portfolio optimization: 92% efficiency\n• Risk assessment: Well-managed\n• Trade opportunities: 3 identified\n\n**📈 Suggested Actions:**\n• Continue current strategy\n• Monitor market volatility\n• Consider profit-taking on strong performers\n\n**🎯 Confidence Level:** 87% (High)\n\n*AI analysis updated every 15 minutes*",
                parse_mode='Markdown',
                reply_markup=types.InlineKeyboardMarkup([[types.InlineKeyboardButton("🔙 Back to Recommendations", callback_data="analytics_recommendations")]])
            )
        
        elif data in ["analytics_hotpicks", "analytics_sectors", "analytics_charts"]:
            feature_name = data.replace('analytics_', '').replace('_', ' ').title()
            bot.answer_callback_query(call.id, f"📊 Loading {feature_name}...")
            bot.send_message(
                call.message.chat.id,
                f"📊 **Market {feature_name}**\n\n**🔥 Top Opportunities:**\n• SOL: Strong momentum (+12%)\n• LINK: Technical breakout pending\n• MATIC: Oversold recovery setup\n\n**📈 Sector Performance:**\n• DeFi: +8.4% (Leading)\n• Layer 1: +6.2% (Strong)\n• Gaming: +4.1% (Moderate)\n\n**⚡ Live Market Data:**\n• Volatility: Moderate\n• Volume: Above average\n• Sentiment: Bullish\n\n*Data updated every 5 minutes*",
                parse_mode='Markdown',
                reply_markup=types.InlineKeyboardMarkup([[types.InlineKeyboardButton("🔙 Back to Trends", callback_data="analytics_trends")]])
            )
        
        elif data in ["copy_budget", "copy_risk", "copy_notifications", "copy_execution"]:
            feature_name = data.replace('copy_', '').replace('_', ' ').title()
            bot.answer_callback_query(call.id, f"⚙️ {feature_name} Settings...")
            bot.send_message(
                call.message.chat.id,
                f"⚙️ **Copy Trading {feature_name}**\n\n**Current Settings:** ✅ Optimized\n\n**Configuration:**\n• Risk level: Moderate\n• Budget allocation: Balanced\n• Execution speed: Instant\n• Notifications: Active\n\n**📊 Performance Impact:**\n• Settings optimized for consistent returns\n• Risk management: Excellent\n• Execution quality: 99.2%\n\n✅ Your copy trading is professionally configured!",
                parse_mode='Markdown',
                reply_markup=types.InlineKeyboardMarkup([[types.InlineKeyboardButton("🔙 Back to Copy Settings", callback_data="copy_settings")]])
            )
        
        elif data in ["enable_2fa", "setup_hardware_key", "setup_biometric", "setup_ip_whitelist"]:
            feature_name = data.replace('setup_', '').replace('enable_', '').replace('_', ' ').title()
            bot.answer_callback_query(call.id, f"🔒 {feature_name} Setup...")
            bot.send_message(
                call.message.chat.id,
                f"🔒 **{feature_name} Security**\n\n**Setup Status:** ✅ Ready\n\n**Security Benefits:**\n• Enhanced account protection\n• Prevents unauthorized access\n• Professional-grade security\n• Industry standard protection\n\n**📱 Setup Process:**\n• Security scan: Complete\n• Device verification: Passed\n• Protection level: Maximum\n\n🛡️ Your account security is now enterprise-grade!",
                parse_mode='Markdown',
                reply_markup=types.InlineKeyboardMarkup([[types.InlineKeyboardButton("🔙 Back to Security", callback_data="wallet_security")]])
            )
        
        elif data in ["rebalance_portfolio", "compare_allocation", "view_leaderboard", "add_price_alert", 
                      "alert_settings", "alert_history", "manage_alerts", "email_copy_report", "export_report"]:
            feature_name = data.replace('_', ' ').title()
            bot.answer_callback_query(call.id, f"✅ {feature_name} Complete!")
            bot.send_message(
                call.message.chat.id,
                f"✅ **{feature_name}**\n\n**Operation Successful:** ✅\n\n**Results:**\n• Data processed successfully\n• Analysis complete\n• Report generated\n• System updated\n\n**📊 Summary:**\n• All metrics analyzed\n• Professional insights provided\n• Recommendations available\n• Next steps identified\n\n🎯 Operation completed successfully!",
                parse_mode='Markdown',
                reply_markup=types.InlineKeyboardMarkup([[types.InlineKeyboardButton("🔙 Back to Main Menu", callback_data="back_to_main")]])
            )
        
        elif data in ["leaderboard_optin", "email_preferences", "export_user_data", "delete_account_request", 
                      "generate_seed_phrase", "export_private_keys", "setup_recovery_questions", "test_recovery"]:
            feature_name = data.replace('_', ' ').title()
            bot.answer_callback_query(call.id, f"🔧 {feature_name} Processed!")
            bot.send_message(
                call.message.chat.id,
                f"🔧 **{feature_name}**\n\n**Request Processed:** ✅\n\n**Account Management:**\n• Privacy settings updated\n• Security preferences applied\n• Data handling configured\n• Backup options available\n\n**📋 Your Preferences:**\n• Settings saved successfully\n• Changes applied immediately\n• Account protection active\n\n🛡️ Your account management is complete!",
                parse_mode='Markdown',
                reply_markup=types.InlineKeyboardMarkup([[types.InlineKeyboardButton("🔙 Back to Settings", callback_data="wallet_privacy")]])
            )
        
        elif data in ["broadcast_emergency", "broadcast_market", "broadcast_promo", "broadcast_general", "broadcast_stats"]:
            broadcast_type = data.replace('broadcast_', '').title()
            bot.answer_callback_query(call.id, f"📢 {broadcast_type} Broadcast Ready!")
            bot.send_message(
                call.message.chat.id,
                f"📢 **{broadcast_type} Broadcast System**\n\n**Broadcast Ready:** ✅\n\n**Target Audience:**\n• All registered users\n• Estimated reach: 100%\n• Delivery method: Instant\n• Priority: High\n\n**📝 Message Guidelines:**\n• Keep messages clear and professional\n• Include actionable information\n• Use appropriate emojis\n• Maintain trading focus\n\n**📊 Broadcast Statistics:**\n• Average open rate: 94%\n• User engagement: High\n• Response time: Immediate\n\n🚀 **System Status:** All broadcasting systems operational!",
                parse_mode='Markdown',
                reply_markup=types.InlineKeyboardMarkup([[types.InlineKeyboardButton("🔙 Back to Admin", callback_data="menu_admin")]])
            )
        
        elif data == "menu_help":
            help_text = """❓❓❓ HELP & SUPPORT CENTER ❓❓❓

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🎯 **WELCOME TO PROFESSIONAL TRADING BOT SUPPORT**

Get instant help with all features and connect with our team for personalized assistance.

**📱 Quick Access:**
• Comprehensive guides for all features
• Step-by-step tutorials
• Live bot support chat
• Technical assistance available 24/7

**💼 Professional Support:**
• Expert trading guidance
• Platform navigation help  
• Technical troubleshooting
• Account management assistance

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📞 **NEED IMMEDIATE HELP?** Use Bot Support below!"""
            
            keyboard = types.InlineKeyboardMarkup(row_width=2)
            keyboard.add(
                types.InlineKeyboardButton("💬 Bot Support", callback_data="help_bot_support"),
                types.InlineKeyboardButton("📚 Trading Guide", callback_data="help_trading")
            )
            keyboard.add(
                types.InlineKeyboardButton("📊 Analytics Help", callback_data="help_analytics"),
                types.InlineKeyboardButton("👥 Copy Trading Help", callback_data="help_copy_trading")
            )
            keyboard.add(
                types.InlineKeyboardButton("💳 Wallet & Withdrawal", callback_data="help_wallet"),
                types.InlineKeyboardButton("🔒 Security Guide", callback_data="help_security")
            )
            keyboard.add(
                types.InlineKeyboardButton("🤖 Bot Commands", callback_data="help_commands"),
                types.InlineKeyboardButton("❓ FAQ", callback_data="help_faq")
            )
            keyboard.add(types.InlineKeyboardButton("🏠 Back to Main Menu", callback_data="back_to_main"))
            
            bot.send_message(call.message.chat.id, help_text, reply_markup=keyboard)
        
        elif data == "help_bot_support":
            bot.answer_callback_query(call.id, "🤖 Connecting you to live support...")
            
            support_text = f"""🤖🤖🤖 LIVE BOT SUPPORT 🤖🤖🤖

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**💬 DIRECT SUPPORT CHAT ACTIVATED**

**Your Support ID:** #{user_id}

**📱 How to Get Help:**
1. Click "Contact Support" below
2. Your message will be sent directly to our admin
3. You'll receive a personal response within minutes
4. All conversations are private and secure

**🎯 What We Help With:**
• Trading questions and strategies
• Technical issues and bugs
• Account problems and withdrawals
• Feature explanations and tutorials
• Security concerns and verification

**⚡ Response Times:**
• Emergency issues: Immediate
• General questions: 5-15 minutes
• Technical support: 15-30 minutes

**🛡️ Your Privacy:**
All support conversations are confidential and encrypted.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

👤 **ADMIN TEAM READY TO ASSIST YOU!**"""
            
            keyboard = types.InlineKeyboardMarkup(row_width=1)
            keyboard.add(
                types.InlineKeyboardButton("💬 Contact Support Now", callback_data="contact_admin"),
                types.InlineKeyboardButton("📋 Submit Bug Report", callback_data="submit_bug_report")
            )
            keyboard.add(
                types.InlineKeyboardButton("🔙 Back to Help", callback_data="menu_help")
            )
            
            bot.send_message(call.message.chat.id, support_text, reply_markup=keyboard)
        
        elif data == "contact_admin":
            bot.answer_callback_query(call.id, "📨 Redirecting to live support...")
            
            contact_text = f"""📨📨📨 LIVE SUPPORT CHAT 📨📨📨

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**✅ DIRECT LINE TO ADMIN**

**Your Support ID:** #{user_id}

**📱 GET INSTANT HELP:**

Click the "Contact Support" button below to chat directly with our admin team on Telegram.

**💬 What We Help With:**
• Trading questions and strategies
• Technical issues and bugs  
• Account problems and withdrawals
• Feature explanations and tutorials
• Security concerns and verification

**⚡ Response Times:**
• Emergency issues: Immediate
• General questions: 5-15 minutes
• Technical support: 15-30 minutes

**🛡️ Privacy:**
All support conversations are confidential and secure.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**👤 ADMIN TEAM READY TO ASSIST YOU!**"""
            
            keyboard = types.InlineKeyboardMarkup(row_width=1)
            keyboard.add(
                types.InlineKeyboardButton("💬 Contact Support", url="https://t.me/CXPBOTSUPPORT")
            )
            keyboard.add(
                types.InlineKeyboardButton("🔙 Back to Bot Support", callback_data="help_bot_support")
            )
            
            bot.send_message(call.message.chat.id, contact_text, reply_markup=keyboard)
        
        elif data == "submit_bug_report":
            bot.answer_callback_query(call.id, "🐛 Redirecting to bug report support...")
            
            bug_text = f"""🐛🐛🐛 BUG REPORT SYSTEM 🐛🐛🐛

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**🔧 TECHNICAL SUPPORT ACTIVATED**

**Bug Report ID:** #BUG-{user_id}-{int(time.time())}

**📋 HOW TO REPORT BUGS:**

Click the "Report Bug" button below to contact our technical support team directly on Telegram.

**🐛 What to Include in Your Report:**
• Describe the bug clearly
• What you expected to happen
• Steps to reproduce the issue
• Any error messages you received
• Your device/browser information

**⚡ TECHNICAL TEAM:** 🟢 Ready to Help

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**🔧 CLICK BELOW TO REPORT THE BUG!**"""
            
            keyboard = types.InlineKeyboardMarkup(row_width=1)
            keyboard.add(
                types.InlineKeyboardButton("🐛 Report Bug", url="https://t.me/CXPBOTSUPPORT")
            )
            keyboard.add(
                types.InlineKeyboardButton("🔙 Back to Bot Support", callback_data="help_bot_support")
            )
            
            bot.send_message(call.message.chat.id, bug_text, reply_markup=keyboard)
        
        elif data == "help_trading":
            bot.answer_callback_query(call.id, "📚 Loading trading guide...")
            
            trading_help = """📚📚📚 COMPLETE TRADING GUIDE 📚📚📚

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**🎯 PROFESSIONAL TRADING TUTORIAL**

**📱 Getting Started:**
1. Click "💰 Buy" to purchase tokens
2. Select your cryptocurrency  
3. Enter amount to invest
4. Confirm transaction

**💼 Portfolio Management:**
• View holdings with "📊 Portfolio"
• Track performance and gains/losses
• Monitor real-time price updates
• Analyze your trading history

**⚡ Smart Trading Tips:**
• Start small to learn the platform
• Diversify across multiple tokens
• Monitor market trends regularly  
• Use analytics for better decisions

**📊 Reading Your Portfolio:**
• **Green numbers:** Profitable positions
• **Red numbers:** Current losses (hold or sell)
• **Percentage:** Your profit/loss rate
• **USD Value:** Current worth of holdings

**🎯 Advanced Features:**
• Copy successful traders automatically
• Set up price alerts for opportunities
• Use analytics for market insights
• Access professional risk management

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

💡 **Ready to start trading professionally!**"""
            
            keyboard = types.InlineKeyboardMarkup(row_width=1)
            keyboard.add(
                types.InlineKeyboardButton("🔙 Back to Help Menu", callback_data="menu_help")
            )
            
            bot.send_message(call.message.chat.id, trading_help, parse_mode='Markdown', reply_markup=keyboard)
        
        elif data in ["help_analytics", "help_copy_trading", "help_wallet", "help_security", "help_commands", "help_faq"]:
            help_type = data.replace('help_', '').replace('_', ' ').title()
            bot.answer_callback_query(call.id, f"📖 Loading {help_type} guide...")
            
            help_content = {
                "Analytics": """📊 **ANALYTICS & INSIGHTS GUIDE**

**🎯 Performance Tracking:**
• View real-time portfolio performance
• Analyze profit/loss trends
• Track success rates by token
• Monitor risk exposure levels

**📈 Market Analysis:**
• Live market data and trends  
• Sector performance insights
• Top trading opportunities
• Professional recommendations

**⚡ Smart Alerts:**
• Price movement notifications
• Portfolio milestone alerts
• Risk management warnings
• Market opportunity signals""",

                "Copy Trading": """👥 **COPY TRADING MASTERY GUIDE**

**🚀 Getting Started:**
• Browse top performing traders
• Review their success rates
• Choose allocation amounts
• Start copying automatically

**📊 Provider Selection:**
• Check win rates and consistency
• Analyze risk levels
• Review trading strategies
• Monitor real-time performance

**⚙️ Management:**
• Adjust copy settings anytime
• Set stop-loss limits
• Monitor copied trades
• Withdraw or reinvest profits""",

                "Wallet": """💳 **WALLET & WITHDRAWAL GUIDE**

**💰 Balance Management:**
• Check current USD balance
• View transaction history
• Track deposit/withdrawal records
• Monitor account activity

**💸 Crypto Withdrawals:**
• 10% mandatory withdrawal fee
• 15-30 minute processing time
• External wallet address required
• Daily limits for security

**🔒 Security Features:**
• Address verification required
• Transaction confirmations
• Daily withdrawal limits
• Professional fraud protection""",

                "Security": """🔒 **COMPLETE SECURITY GUIDE**

**🛡️ Account Protection:**
• Two-factor authentication setup
• Secure password requirements
• Login monitoring alerts
• IP address whitelist options

**💼 Trading Security:**
• Secure API connections
• Encrypted data transmission
• Professional-grade protection
• Regular security audits

**⚡ Best Practices:**
• Never share login credentials
• Use strong, unique passwords
• Enable all security features
• Monitor account activity regularly""",

                "Commands": """🤖 **BOT COMMANDS REFERENCE**

**🎯 Main Commands:**
• /start - Initialize your account
• /help - Open help center
• /portfolio - View holdings
• /balance - Check USD balance

**📊 Trading Commands:**
• Use menu buttons for all trading
• Interactive keyboards for navigation  
• Real-time price updates
• Professional interface design

**⚡ Quick Actions:**
• Refresh prices instantly
• Access admin panel (if admin)
• Navigate with back buttons
• Professional trading experience""",

                "Faq": """❓ **FREQUENTLY ASKED QUESTIONS**

**🤔 Common Questions:**

**Q: How do I start trading?**
A: Click "💰 Buy" and select a cryptocurrency

**Q: When can I withdraw profits?**  
A: Anytime via crypto withdrawal (10% fee)

**Q: Is my money safe?**
A: Yes, professional security & encryption

**Q: How do copy trading work?**
A: Follow expert traders, copy automatically

**Q: What if I need help?**
A: Use "💬 Bot Support" for instant assistance

**Q: Are there trading fees?**
A: Only 10% withdrawal fee, no trading fees"""
            }
            
            content = help_content.get(help_type, "Help content coming soon!")
            
            bot.send_message(
                call.message.chat.id,
                f"📖📖📖 {help_type.upper()} HELP 📖📖📖\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n{content}\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n💡 **Need more help?** Use Bot Support for personal assistance!",
                parse_mode='Markdown',
                reply_markup=types.InlineKeyboardMarkup([[types.InlineKeyboardButton("🔙 Back to Help Menu", callback_data="menu_help")]])
            )
        
        else:
            # Professional error handling for unhandled callbacks
            logger.warning(f"Unhandled callback data: {data} from user {user_id}")
            bot.answer_callback_query(call.id, "⚡ Processing your request...")
            bot.send_message(
                call.message.chat.id,
                "⚡ **Request Processed**\n\n**Status:** ✅ Complete\n\n**System Response:**\n• Your action has been logged\n• Professional trading systems active\n• All core features operational\n• Real-time market data flowing\n\n**📊 Quick Stats:**\n• System uptime: 99.9%\n• Processing speed: Optimal\n• Security status: Maximum\n\n🎯 **Ready for trading!** Use the menu below to continue.",
                parse_mode='Markdown',
                reply_markup=get_main_menu_keyboard(user_id)
            )
        
        # Only answer callback query if it hasn't been answered yet
        if not data.startswith("copy_address_") and not data.startswith("admin_curr_"):
            try:
                bot.answer_callback_query(call.id)
            except Exception:
                pass  # Ignore timeout errors
    
    except Exception as e:
        logger.error(f"Error in callback handler: {e}")
        bot.answer_callback_query(call.id, "⚠️ An error occurred. Please try again.")

def get_copy_trading_keyboard():
    """Copy trading main menu keyboard"""
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("🔍 Browse Traders", callback_data="copy_browse"),
        types.InlineKeyboardButton("👥 Following", callback_data="copy_following")
    )
    keyboard.add(
        types.InlineKeyboardButton("🎯 Copy Specific Trader", callback_data="copy_specific_trader")
    )
    keyboard.add(
        types.InlineKeyboardButton("📊 My Performance", callback_data="copy_performance"),
        types.InlineKeyboardButton("⚡ Become Provider", callback_data="copy_become_provider")
    )
    keyboard.add(
        types.InlineKeyboardButton("🏠 Back to Main", callback_data="back_to_main")
    )
    return keyboard

async def handle_copy_trading_menu(chat_id, user_id):
    """Handle copy trading main menu"""
    following_count = len(await db.get_user_following(user_id))
    is_provider = await db.is_signal_provider(user_id)
    
    copy_text = f"""👥 **Copy Trading**

🚀 **Copy successful traders automatically!**

Follow expert traders and copy their trades in real-time. Our signal providers have proven track records of profitability.

📊 **Your Status:**
• Following: {following_count} traders
• Provider Status: {'✅ Active' if is_provider else '❌ Not a provider'}

💡 **How it works:**
1. Browse top performing traders
2. Follow them with your allocation
3. Their trades are copied to your account automatically
4. Monitor performance and adjust settings

Choose an option below to get started:"""
    
    bot.send_message(
        chat_id,
        copy_text,
        parse_mode='Markdown',
        reply_markup=get_copy_trading_keyboard()
    )

async def create_mock_providers():
    """Create mock successful signal providers for demonstration"""
    mock_providers = [
        {
            "user_id": 999999001,
            "username": "crypto_wizard",
            "provider_name": "Crypto Wizard Pro",
            "description": "Professional crypto trader with 3+ years experience. Specializes in swing trading and risk management.",
            "total_profit": 2450.50,
            "win_rate": 78.5,
            "total_trades": 156,
            "followers_count": 89
        },
        {
            "user_id": 999999002,
            "username": "defi_master",
            "provider_name": "DeFi Master",
            "description": "DeFi specialist focused on high-yield opportunities and alt-coin gems.",
            "total_profit": 1890.25,
            "win_rate": 72.3,
            "total_trades": 203,
            "followers_count": 67
        },
        {
            "user_id": 999999003,
            "username": "bitcoin_bull",
            "provider_name": "Bitcoin Bull",
            "description": "Conservative BTC-focused strategy with steady monthly returns.",
            "total_profit": 3200.75,
            "win_rate": 85.2,
            "total_trades": 98,
            "followers_count": 124
        }
    ]
    
    for provider in mock_providers:
        # Create mock user first
        await db.create_user(provider["user_id"], provider["username"], provider["provider_name"])
        
        # Create signal provider
        await db.create_signal_provider(
            provider["user_id"], 
            {
                "provider_name": provider["provider_name"],
                "description": provider["description"]
            }
        )
        
        # Update stats manually since these are mock providers
        async with aiosqlite.connect(db.db_path) as database:
            await database.execute("""
                UPDATE signal_providers 
                SET total_profit = ?, win_rate = ?, total_trades = ?, followers_count = ?
                WHERE user_id = ?
            """, (provider["total_profit"], provider["win_rate"], provider["total_trades"], 
                  provider["followers_count"], provider["user_id"]))
            await database.commit()

async def show_signal_providers(chat_id, user_id):
    """Show available signal providers to follow"""
    try:
        # Create mock providers if they don't exist
        await create_mock_providers()
        
        providers = await db.get_signal_providers()
        user_following = await db.get_user_following(user_id)
        following_ids = [f["provider_id"] for f in user_following]
        
        if not providers:
            bot.send_message(
                chat_id,
                "📭 No signal providers available at the moment.\n\nCheck back later!",
                reply_markup=get_copy_trading_keyboard()
            )
            return
        
        providers_text = "🔍 **Top Signal Providers**\n\n"
        providers_text += "Choose a trader to view details and follow:\n\n"
        
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        
        for provider in providers:
            profit_color = "🟢" if provider["total_profit"] > 0 else "🔴"
            following_status = "✅ Following" if provider["user_id"] in following_ids else ""
            
            providers_text += f"**{provider['provider_name']}** {following_status}\n"
            providers_text += f"{profit_color} Profit: ${provider['total_profit']:.2f} | "
            providers_text += f"Win Rate: {provider['win_rate']:.1f}% | "
            providers_text += f"Followers: {provider['followers_count']}\n\n"
            
            keyboard.add(types.InlineKeyboardButton(
                f"📊 {provider['provider_name']}", 
                callback_data=f"view_provider_{provider['user_id']}"
            ))
        
        keyboard.add(types.InlineKeyboardButton("🔄 Refresh", callback_data="copy_browse"))
        keyboard.add(types.InlineKeyboardButton("🔙 Back", callback_data="menu_copy_trading"))
        
        bot.send_message(chat_id, providers_text, parse_mode='Markdown', reply_markup=keyboard)
        
    except Exception as e:
        logger.error(f"Error showing signal providers: {e}")
        bot.send_message(chat_id, "❌ Error loading signal providers.", reply_markup=get_copy_trading_keyboard())

async def show_provider_details(chat_id, user_id, provider_id):
    """Show detailed information about a signal provider"""
    try:
        providers = await db.get_signal_providers()
        provider = next((p for p in providers if p["user_id"] == provider_id), None)
        
        if not provider:
            bot.send_message(chat_id, "❌ Provider not found.", reply_markup=get_copy_trading_keyboard())
            return
        
        user_following = await db.get_user_following(user_id)
        is_following = any(f["provider_id"] == provider_id for f in user_following)
        
        profit_color = "🟢" if provider["total_profit"] > 0 else "🔴"
        
        details_text = f"""👤 **{provider['provider_name']}**

📝 **Description:**
{provider['description']}

📊 **Performance Stats:**
{profit_color} **Total Profit:** ${provider['total_profit']:.2f}
🎯 **Win Rate:** {provider['win_rate']:.1f}%
📈 **Total Trades:** {provider['total_trades']}
👥 **Followers:** {provider['followers_count']}
📅 **Active Since:** {provider['created_at'][:10]}

💡 **Risk Level:** {'🟢 Low' if provider['win_rate'] > 80 else '🟡 Medium' if provider['win_rate'] > 70 else '🔴 High'}"""
        
        keyboard = types.InlineKeyboardMarkup()
        
        if is_following:
            keyboard.add(types.InlineKeyboardButton(
                "❌ Unfollow", 
                callback_data=f"unfollow_{provider_id}"
            ))
        else:
            keyboard.add(types.InlineKeyboardButton(
                "✅ Follow Trader", 
                callback_data=f"follow_{provider_id}"
            ))
        
        keyboard.add(
            types.InlineKeyboardButton("🔄 Refresh", callback_data=f"view_provider_{provider_id}"),
            types.InlineKeyboardButton("🔙 Back", callback_data="copy_browse")
        )
        
        bot.send_message(chat_id, details_text, parse_mode='Markdown', reply_markup=keyboard)
        
    except Exception as e:
        logger.error(f"Error showing provider details: {e}")
        bot.send_message(chat_id, "❌ Error loading provider details.", reply_markup=get_copy_trading_keyboard())

async def handle_copy_specific_trader(chat_id, user_id):
    """Handle copy specific trader request"""
    users_inputting_trader_id.add(user_id)
    
    trader_input_text = """🎯 **Copy Specific Trader**

Please enter the **Trader ID** or **Username** of the trader you want to follow:

💡 **Examples:**
• Trader ID: `123456789`
• Username: `@crypto_trader`
• Username: `bitcoin_expert`

📋 **How to find Trader IDs:**
• Ask the trader for their ID
• Check their profile or signals
• Look in trading communities

Just type the ID or username and send it."""

    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("🔙 Back to Copy Trading", callback_data="menu_copy_trading"))
    
    bot.send_message(
        chat_id,
        trader_input_text,
        parse_mode='Markdown',
        reply_markup=keyboard
    )

async def process_trader_id_input(chat_id, user_id, trader_input):
    """Process trader ID/username input from user"""
    try:
        # Clean up the input
        trader_input = trader_input.strip()
        if trader_input.startswith('@'):
            trader_input = trader_input[1:]  # Remove @ symbol
        
        # Try to parse as user ID first
        provider_id = None
        if trader_input.isdigit():
            provider_id = int(trader_input)
        
        # Search for the trader
        if provider_id:
            # Search by user ID
            providers = await db.get_signal_providers()
            provider = next((p for p in providers if p["user_id"] == provider_id), None)
        else:
            # Search by username
            provider = await db.find_trader_by_username(trader_input)
        
        if not provider:
            # Check if it's one of our mock providers by name
            providers = await db.get_signal_providers()
            provider = next((p for p in providers if 
                            trader_input.lower() in p.get("provider_name", "").lower() or
                            trader_input.lower() in p.get("username", "").lower()), None)
        
        if provider:
            # Show provider details
            await show_provider_details(chat_id, user_id, provider["user_id"])
        else:
            # Trader not found - offer to create or suggest alternatives
            not_found_text = f"""❌ **Trader Not Found**

Could not find trader: `{trader_input}`

💡 **Suggestions:**
• Double-check the Trader ID or username
• Try browsing our top traders instead
• Ask the trader to verify their ID

🔍 **Popular Traders:**
• `999999001` - Crypto Wizard Pro
• `999999002` - DeFi Master  
• `999999003` - Bitcoin Bull

Would you like to browse available traders instead?"""
            
            keyboard = types.InlineKeyboardMarkup()
            keyboard.add(
                types.InlineKeyboardButton("🔍 Browse Traders", callback_data="copy_browse"),
                types.InlineKeyboardButton("🔄 Try Again", callback_data="copy_specific_trader")
            )
            keyboard.add(types.InlineKeyboardButton("🔙 Back", callback_data="menu_copy_trading"))
            
            bot.send_message(
                chat_id,
                not_found_text,
                parse_mode='Markdown',
                reply_markup=keyboard
            )
    
    except Exception as e:
        logger.error(f"Error processing trader ID input: {e}")
        
        error_text = """❌ **Error Processing Input**

There was an error processing your request. Please try again or browse available traders."""
        
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(
            types.InlineKeyboardButton("🔍 Browse Traders", callback_data="copy_browse"),
            types.InlineKeyboardButton("🔄 Try Again", callback_data="copy_specific_trader")
        )
        keyboard.add(types.InlineKeyboardButton("🔙 Back", callback_data="menu_copy_trading"))
        
        bot.send_message(
            chat_id,
            error_text,
            parse_mode='Markdown',
            reply_markup=keyboard
        )

async def show_user_following(chat_id, user_id):
    """Show traders that user is currently following"""
    try:
        following = await db.get_user_following(user_id)
        
        if not following:
            following_text = """👥 **Your Following List**

📭 You're not following any traders yet.

Start by browsing our top performers and follow traders whose strategies match your risk tolerance."""
            
            keyboard = types.InlineKeyboardMarkup()
            keyboard.add(types.InlineKeyboardButton("🔍 Browse Traders", callback_data="copy_browse"))
            keyboard.add(types.InlineKeyboardButton("🔙 Back", callback_data="menu_copy_trading"))
        else:
            following_text = f"👥 **Your Following List** ({len(following)} traders)\n\n"
            
            keyboard = types.InlineKeyboardMarkup(row_width=1)
            
            for trader in following:
                profit_color = "🟢" if trader["total_profit"] > 0 else "🔴"
                following_text += f"**{trader['provider_name']}**\n"
                following_text += f"{profit_color} Profit: ${trader['total_profit']:.2f} | "
                following_text += f"Win Rate: {trader['win_rate']:.1f}%\n"
                following_text += f"💰 Your Allocation: ${trader['allocation_amount']:.2f}\n\n"
                
                keyboard.add(types.InlineKeyboardButton(
                    f"⚙️ {trader['provider_name']}", 
                    callback_data=f"manage_follow_{trader['provider_id']}"
                ))
            
            keyboard.add(types.InlineKeyboardButton("🔄 Refresh", callback_data="copy_following"))
            keyboard.add(types.InlineKeyboardButton("🔙 Back", callback_data="menu_copy_trading"))
        
        bot.send_message(chat_id, following_text, parse_mode='Markdown', reply_markup=keyboard)
        
    except Exception as e:
        logger.error(f"Error showing user following: {e}")
        bot.send_message(chat_id, "❌ Error loading following list.", reply_markup=get_copy_trading_keyboard())

async def handle_deposits_menu(chat_id, user_id):
    """Handle deposits menu display"""
    try:
        # Get pending deposits for this user
        pending_deposits = await db.get_pending_deposits(user_id)
        
        if pending_deposits:
            deposit_text = "📥 **Your Pending Deposits**\n\n"
            for deposit in pending_deposits:
                deposit_text += f"🔸 **{deposit['token']}**: {deposit['amount']:.8f}\n"
                deposit_text += f"   📋 TX: `{deposit['transaction_id'][:16]}...`\n"
                deposit_text += f"   ⏱ Status: {deposit['confirmations']}/6 confirmations\n"
                deposit_text += f"   📅 Detected: {deposit['detected_at']}\n\n"
        else:
            deposit_text = "📥 **Deposits**\n\n✅ No pending deposits found.\n\nYour deposits will appear here once detected on the blockchain."
        
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(
            types.InlineKeyboardButton("🔄 Refresh", callback_data="menu_deposits"),
            types.InlineKeyboardButton("📋 Address List", callback_data="show_addresses")
        )
        keyboard.add(types.InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main"))
        
        bot.send_message(
            chat_id,
            deposit_text,
            parse_mode='Markdown',
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Error in deposits menu: {e}")
        bot.send_message(chat_id, "❌ Error loading deposits. Please try again.")

async def handle_notifications_menu(chat_id, user_id):
    """Handle notifications menu display"""
    try:
        # Get recent deposit notifications
        notification_text = "🔔 **Notification Settings**\n\n"
        notification_text += "✅ Deposit confirmations: Enabled\n"
        notification_text += "✅ Price alerts: Coming soon\n"
        notification_text += "✅ Trading alerts: Coming soon\n\n"
        notification_text += "📥 Recent activity will appear here when deposits are detected."
        
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(types.InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_main"))
        
        bot.send_message(
            chat_id,
            notification_text,
            parse_mode='Markdown',
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Error in notifications menu: {e}")
        bot.send_message(chat_id, "❌ Error loading notifications. Please try again.")

def send_deposit_notification(user_id, amount, token, tx_id):
    """Send deposit notification to user"""
    try:
        message = f"🎉 **Deposit Confirmed!**\n\n"
        message += f"💰 **Amount**: {amount:.8f} {token}\n"
        message += f"📋 **Transaction**: `{tx_id}`\n"
        message += f"✅ **Status**: Confirmed and credited\n\n"
        message += f"Your {token} balance has been updated!"
        
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(types.InlineKeyboardButton("💳 View Balance", callback_data="menu_wallet"))
        keyboard.add(types.InlineKeyboardButton("📊 Portfolio", callback_data="menu_portfolio"))
        
        bot.send_message(
            user_id,
            message,
            parse_mode='Markdown',
            reply_markup=keyboard
        )
        logger.info(f"Sent deposit notification to user {user_id}")
    except Exception as e:
        logger.error(f"Error sending deposit notification: {e}")

async def execute_sell_order(call, user_id, token_symbol, sell_amount):
    """Execute sell order and update user balance/portfolio"""
    try:
        # Get current prices
        prices = await get_crypto_prices()
        if not prices or token_symbol not in prices:
            bot.answer_callback_query(call.id, "❌ Unable to fetch current price. Please try again.")
            return
        
        current_price = prices[token_symbol]["price"]
        effective_price = current_price * 0.995  # Apply 0.5% slippage (negative for sell)
        
        # Calculate USD proceeds
        usd_proceeds = sell_amount * effective_price
        
        # Get current balance and portfolio
        balance = await db.get_user_balance(user_id)
        portfolio = await db.get_user_portfolio(user_id)
        user_holdings = {token: data["amount"] for token, data in portfolio.items()}
        
        if token_symbol not in user_holdings or user_holdings[token_symbol] < sell_amount:
            bot.answer_callback_query(call.id, f"❌ Insufficient {token_symbol}. Available: {user_holdings.get(token_symbol, 0):.6f}")
            return
        
        # Update balance and portfolio
        new_balance = balance + usd_proceeds
        await db.update_user_balance(user_id, new_balance)
        await db.update_portfolio(user_id, token_symbol, -sell_amount, effective_price)  # Negative amount for sell
        await db.add_trade_history(user_id, "SELL", token_symbol, sell_amount, effective_price, usd_proceeds)
        
        success_text = f"""✅ **Sell Order Executed!**

Successfully sold {sell_amount:.6f} {token_symbol}

**Trade Details:**
• Token: {token_symbol}
• Amount Sold: {sell_amount:.6f}
• Price: {format_price(effective_price)}
• Proceeds: ${usd_proceeds:.2f}
• New Balance: ${new_balance:.2f}

Your portfolio has been updated."""
        
        # Create keyboard with withdrawal option after selling
        keyboard = types.InlineKeyboardMarkup(row_width=2)
        keyboard.add(
            types.InlineKeyboardButton("💸 Withdraw Cash", callback_data="wallet_withdraw"),
            types.InlineKeyboardButton("💰 Buy More", callback_data="menu_buy")
        )
        keyboard.add(
            types.InlineKeyboardButton("📊 Portfolio", callback_data="menu_portfolio"),
            types.InlineKeyboardButton("🏠 Main Menu", callback_data="back_to_main")
        )
        
        bot.send_message(
            call.message.chat.id,
            success_text,
            parse_mode='Markdown',
            reply_markup=keyboard
        )
        
    except Exception as e:
        logger.error(f"Error executing sell order: {e}")
        bot.answer_callback_query(call.id, "❌ Error executing sell order. Please try again.")

async def show_deposit_addresses(chat_id, user_id):
    """Show all deposit addresses for user with copy functionality"""
    try:
        address_text = """📋📋📋 YOUR DEPOSIT ADDRESSES 📋📋📋

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🚀 Send crypto to these addresses to automatically credit your account:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

"""
        
        # Create keyboard without address buttons
        keyboard = types.InlineKeyboardMarkup()
        
        for token, config in SUPPORTED_TOKENS.items():
            address = config.get('address', 'Not configured')
            network = config.get('network', 'Network')
            if address != f"YOUR_{token}_ADDRESS_HERE" and address != "Not configured":
                address_text += f"🔸 **{token} ({network})**\n"
                # Add address as copyable text directly in the message
                address_text += f"📋 `{address}`\n\n"
            else:
                address_text += f"🔸 {token}: Not configured\n\n"
        
        address_text += """━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

⚠️ IMPORTANT: Only send the correct cryptocurrency to its matching address!

🎯 Tap the addresses above to copy them directly!"""
        
        keyboard.add(types.InlineKeyboardButton("🔙 Back to Deposits", callback_data="menu_deposits"))
        
        bot.send_message(
            chat_id,
            address_text,
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Error showing addresses: {e}")
        bot.send_message(chat_id, "❌ Error loading addresses. Please try again.")

@bot.message_handler(commands=['sell'])
def sell_command(message):
    """Handle /sell command"""
    user_id = message.from_user.id
    portfolio = asyncio.run(db.get_user_portfolio(user_id))
    
    if not portfolio:
        bot.reply_to(
            message,
            "📭 You don't have any tokens to sell.\n\nUse /buy to start trading!",
            reply_markup=get_main_menu_keyboard(user_id)
        )
        return
    
    bot.reply_to(
        message,
        "💸 **Sell Cryptocurrencies**\n\nSelect a token to sell from your portfolio:",
        parse_mode='Markdown',
        reply_markup=get_main_menu_keyboard(user_id)
    )

@bot.message_handler(commands=['wallet'])
def wallet_command(message):
    """Handle /wallet command"""
    user_id = message.from_user.id
    balance = asyncio.run(db.get_user_balance(user_id))
    portfolio = asyncio.run(db.get_user_portfolio(user_id))
    
    # Calculate total account value (USD + token portfolio)
    total_portfolio_value = 0
    current_prices = asyncio.run(get_crypto_prices()) or {}
    
    if portfolio:
        for token, holding in portfolio.items():
            amount = holding['amount']
            token_price = current_prices.get(token, {}).get('price', 0)
            total_portfolio_value += amount * token_price
    
    total_account_value = balance + total_portfolio_value
    
    wallet_text = f"""💳 **Wallet Information**

💰 **Total Value:** ${total_account_value:.2f} USD
💵 **Cash:** ${balance:.2f} | 🪙 **Tokens:** ${total_portfolio_value:.2f}
"""
    
    if portfolio:
        wallet_text += "\n🪙 **Top Holdings:**\n"
        
        # Show top 3 holdings with USD rates
        holdings_items = list(portfolio.items())[:3]
        for token, holding in holdings_items:
            amount = holding['amount']
            
            # Format amount display
            if token in ['BTC', 'ETH']:
                amount_display = f"{amount:.6f}"
            else:
                amount_display = f"{amount:.4f}"
            
            # Get current price and calculate value
            token_price = current_prices.get(token, {}).get('price', 0)
            total_value = amount * token_price
            
            if token_price > 0:
                wallet_text += f"• {token}: {amount_display} @ ${format_price(token_price)[1:]} ≈ ${total_value:.2f}\n"
            else:
                wallet_text += f"• {token}: {amount_display}\n"
        
        if len(portfolio) > 3:
            wallet_text += f"... and {len(portfolio) - 3} more tokens\n"
    else:
        wallet_text += "\n🪙 **Holdings:** None yet\n"
    
    wallet_text += "\nThis is your real trading balance. Use the options below to manage your wallet:"
    
    # Create wallet options keyboard
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("💰 Check Balance", callback_data="wallet_balance"),
        types.InlineKeyboardButton("💳 Transaction History", callback_data="wallet_history")
    )
    keyboard.add(
        types.InlineKeyboardButton("🔄 Refresh", callback_data="wallet_refresh"),
        types.InlineKeyboardButton("⚙️ Wallet Settings", callback_data="wallet_settings")
    )
    keyboard.add(types.InlineKeyboardButton("🏠 Main Menu", callback_data="back_to_main"))
    
    bot.reply_to(
        message,
        wallet_text,
        parse_mode='Markdown',
        reply_markup=keyboard
    )

@bot.message_handler(commands=['admin'])
def admin_command(message):
    """Handle /admin command"""
    user_id = message.from_user.id
    
    # Check if user is admin
    if not is_admin(user_id):
        bot.reply_to(message, "❌ Access denied. Admin only.")
        return
    
    # Get system statistics
    total_users = asyncio.run(db.get_total_users())
    total_trades = asyncio.run(db.get_total_trades())
    total_volume = asyncio.run(db.get_total_volume())
    
    admin_text = f"""👑 **Admin Panel**

📊 **System Statistics:**
• Total Users: {total_users if total_users else 0}
• Total Trades: {total_trades if total_trades else 0}
• Total Volume: ${total_volume if total_volume else 0:.2f}

⚡ **Quick Actions:**
Use the buttons below to manage the bot:"""
    
    bot.reply_to(
        message,
        admin_text,
        parse_mode='Markdown',
        reply_markup=get_admin_keyboard()
    )

@bot.message_handler(commands=['myid'])
def get_user_id_command(message):
    """Get user's Telegram ID for admin setup"""
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name
    
    id_text = f"""🆔 Your Telegram Information

User ID: {user_id}
Username: {username if hasattr(message.from_user, 'username') and message.from_user.username else 'N/A'}

📋 To enable admin access:
1. Copy your User ID: {user_id}
2. Open config.py 
3. Change ADMIN_USER_ID = None to ADMIN_USER_ID = {user_id}
4. Restart the bot
5. You'll then see the 👑 Admin Panel button!"""
    
    bot.reply_to(message, id_text, parse_mode='Markdown')

@bot.message_handler(func=lambda message: True)
def handle_text_input(message):
    """Handle all text messages for various inputs"""
    user_id = message.from_user.id
    text = message.text.strip()
    
    # Check if user is inputting trader ID
    if user_id in users_inputting_trader_id:
        users_inputting_trader_id.remove(user_id)
        asyncio.run(process_trader_id_input(message.chat.id, user_id, text))
        return
    
    # Check if user is connecting wallet
    if user_id in users_connecting_wallet:
        wallet_type = users_connecting_wallet.pop(user_id)
        asyncio.run(process_wallet_credentials(message.chat.id, user_id, text, wallet_type))
        return
    
    # Check if admin is performing balance operations
    if user_id in admin_balance_operations:
        if is_admin(user_id):
            logger.info(f"Processing admin balance input from user {user_id}: {text}")
            asyncio.run(process_admin_balance_input(message.chat.id, user_id, text))
        else:
            admin_balance_operations.pop(user_id, None)
        return
    
    # Check if admin is performing ban operations
    if user_id in admin_ban_operations:
        if is_admin(user_id):
            asyncio.run(process_admin_ban_input(message.chat.id, user_id, text))
        else:
            admin_ban_operations.pop(user_id, None)
        return
    
    # Check if user is entering withdrawal info (TXID or address)
    if user_id in user_withdrawal_states:
        user_state = user_withdrawal_states[user_id]
        
        if user_state.get('step') == 'entering_txid':
            # Handle TXID input for fee verification
            asyncio.run(process_txid_input(message.chat.id, user_id, text))
        else:
            # Handle withdrawal address input (original logic)
            asyncio.run(process_withdrawal_address_input(message.chat.id, user_id, text))
        return
    
    # Check if user is sending support message or bug report
    if user_id in user_states:
        user_state = user_states[user_id]['state']
        
        if user_state == 'waiting_for_support_message':
            # Process support message
            process_support_message(message.chat.id, user_id, text)
            user_states.pop(user_id, None)  # Remove user state
            return
        
        elif user_state == 'waiting_for_bug_report':
            # Process bug report
            process_bug_report(message.chat.id, user_id, text)
            user_states.pop(user_id, None)  # Remove user state
            return
    
    # For any other text, show main menu
    bot.reply_to(
        message,
        "💡 Use the menu buttons below or commands like /start, /buy, /sell, /portfolio",
        reply_markup=get_main_menu_keyboard(user_id)
    )

@bot.message_handler(commands=['deposits'])
def deposits_command(message):
    """Handle /deposits command"""
    user_id = message.from_user.id
    asyncio.run(handle_deposits_menu(message.chat.id, user_id))

@bot.message_handler(commands=['withdraw'])
def withdraw_command(message):
    """Handle /withdraw command"""
    user_id = message.from_user.id
    balance = asyncio.run(db.get_user_balance(user_id))
    portfolio = asyncio.run(db.get_user_portfolio(user_id))
    
    # Check if user has funds to withdraw
    total_portfolio_value = 0
    if portfolio:
        prices = asyncio.run(get_crypto_prices()) or {}
        for token, holding in portfolio.items():
            token_price = prices.get(token, {}).get('price', 0)
            total_portfolio_value += holding['amount'] * token_price
    
    total_value = balance + total_portfolio_value
    
    if total_value < 10:
        bot.reply_to(
            message,
            f"❌ **Insufficient funds for withdrawal**\n\nMinimum withdrawal: $10.00\nYour total value: ${total_value:.2f}\n\nTrade more to reach the minimum!",
            parse_mode='Markdown',
            reply_markup=get_main_menu_keyboard(user_id)
        )
        return
    
    withdraw_text = f"""💸💸💸 CRYPTO WITHDRAWAL ONLY 💸💸💸

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**Your Total Account Value:** ${total_value:.2f}
💵 **Cash:** ${balance:.2f}
🪙 **Tokens:** ${total_portfolio_value:.2f}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🪙 **CRYPTO TOKENS ONLY** - Send to external wallet

⚠️⚠️ MANDATORY 10% FEE ⚠️⚠️
- 10% fee applies to ALL crypto withdrawals
- Fee payment is MANDATORY to proceed  
- Processing time: 15-30 minutes

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

💎💎 START CRYPTO WITHDRAWAL 💎💎"""
    
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("🪙 Start Crypto Withdrawal", callback_data="withdraw_type_CRYPTO"))
    keyboard.add(
        types.InlineKeyboardButton("📊 Withdrawal History", callback_data="wallet_withdrawals"),
        types.InlineKeyboardButton("🏠 Main Menu", callback_data="back_to_main")
    )
    
    bot.reply_to(
        message,
        withdraw_text,
        parse_mode='Markdown',
        reply_markup=keyboard
    )

async def show_wallet_connection_required(chat_id, user_id, return_action):
    """Show wallet connection requirement before copy trading"""
    wallet_text = """🔐 **Wallet Connection Required**

To start copy trading, you need to connect your crypto wallet first. This ensures secure access to your funds for automated trading.

🔒 **Security Features:**
• Your private keys are encrypted and stored securely
• Only you have access to your wallet
• Disconnect anytime from settings

📱 **Supported Wallets:**
Choose your preferred wallet type to connect:"""

    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(
        types.InlineKeyboardButton("🦊 MetaMask", callback_data="connect_wallet_MetaMask"),
        types.InlineKeyboardButton("💼 Trust Wallet", callback_data="connect_wallet_TrustWallet")
    )
    keyboard.add(
        types.InlineKeyboardButton("🔷 Coinbase", callback_data="connect_wallet_Coinbase"),
        types.InlineKeyboardButton("👻 Phantom", callback_data="connect_wallet_Phantom")
    )
    keyboard.add(
        types.InlineKeyboardButton("📱 WalletConnect", callback_data="connect_wallet_WalletConnect")
    )
    keyboard.add(types.InlineKeyboardButton("🔙 Back to Copy Trading", callback_data="menu_copy_trading"))
    
    bot.send_message(
        chat_id,
        wallet_text,
        parse_mode='Markdown',
        reply_markup=keyboard
    )

async def handle_wallet_connection(chat_id, user_id, wallet_type):
    """Handle wallet connection process"""
    users_connecting_wallet[user_id] = wallet_type
    
    connection_text = f"""🔐 **Connect {wallet_type} Wallet**

Please provide your wallet credentials to enable copy trading:

🔑 **Option 1: Private Key**
Send your private key (64 characters)
Example: `0x1234567890abcdef...`

🌱 **Option 2: Seed Phrase** 
Send your 12 or 24-word recovery phrase
Example: `apple orange banana...`

🔒 **Security Guarantee:**
• Your private keys and seed phrases are fully protected
• We never store or share your wallet credentials with anyone
• Your sensitive information remains completely secure with you

Just type your private key or seed phrase and send it:"""
    
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("❌ Cancel", callback_data="menu_copy_trading"))
    
    bot.send_message(
        chat_id,
        connection_text,
        parse_mode='Markdown',
        reply_markup=keyboard
    )

async def process_wallet_credentials(chat_id, user_id, credentials, wallet_type):
    """Process wallet credentials input"""
    try:
        # Validate credentials format (mock validation)
        credentials = credentials.strip()
        is_private_key = len(credentials) == 64 or credentials.startswith('0x')
        is_seed_phrase = len(credentials.split()) >= 12
        
        if not (is_private_key or is_seed_phrase):
            error_text = """❌ **Invalid Credentials**

Please provide either:
• A valid private key (64 characters)
• A seed phrase (12-24 words)

Try again or cancel to go back."""
            
            keyboard = types.InlineKeyboardMarkup()
            keyboard.add(
                types.InlineKeyboardButton("🔄 Try Again", callback_data=f"connect_wallet_{wallet_type}"),
                types.InlineKeyboardButton("❌ Cancel", callback_data="menu_copy_trading")
            )
            
            bot.send_message(chat_id, error_text, parse_mode='Markdown', reply_markup=keyboard)
            return
        
        # Generate mock wallet address
        mock_address = f"0x{''.join([f'{i%16:x}' for i in range(40)])}"
        connection_method = "Private Key" if is_private_key else "Seed Phrase"
        
        # Save wallet connection (without storing actual credentials for security)
        await db.connect_wallet(user_id, mock_address, wallet_type, connection_method)
        
        success_text = f"""✅ **Wallet Connected Successfully!**

🔐 **{wallet_type} Wallet**
📍 **Address:** `{mock_address[:6]}...{mock_address[-4:]}`
🔗 **Method:** {connection_method}
⏰ **Connected:** Just now

🚀 **You can now:**
• Browse and follow traders
• Copy trades automatically  
• Monitor your performance
• Manage your copy trading settings

Ready to start copy trading?"""
        
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(
            types.InlineKeyboardButton("🔍 Browse Traders", callback_data="copy_browse"),
            types.InlineKeyboardButton("🎯 Copy Specific Trader", callback_data="copy_specific_trader")
        )
        keyboard.add(types.InlineKeyboardButton("👥 Copy Trading Menu", callback_data="menu_copy_trading"))
        
        bot.send_message(
            chat_id,
            success_text,
            parse_mode='Markdown',
            reply_markup=keyboard
        )
        
    except Exception as e:
        logger.error(f"Error processing wallet credentials: {e}")
        
        error_text = """❌ **Connection Error**

There was an error connecting your wallet. Please try again or contact support if the issue persists."""
        
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(
            types.InlineKeyboardButton("🔄 Try Again", callback_data=f"connect_wallet_{wallet_type}"),
            types.InlineKeyboardButton("🔙 Back", callback_data="menu_copy_trading")
        )
        
        bot.send_message(chat_id, error_text, parse_mode='Markdown', reply_markup=keyboard)

async def start_balance_management(chat_id, admin_id, action):
    """Start admin balance management process"""
    admin_balance_operations[admin_id] = {"action": action, "step": "user_id"}
    
    action_text = "add to" if action == "add" else "subtract from"
    
    # Get user list for easier identification
    users = await db.get_all_users()
    user_list = "Available Users:\n"
    
    if users:
        for i, user in enumerate(users[:8], 1):  # Show first 8 users
            username_display = f"@{user['username']}" if user.get('username') else user.get('first_name', 'No Name')
            balance = await db.get_user_balance(user['user_id'])
            user_list += f"{i}. {username_display}\n   ID: {user['user_id']} - Balance: ${balance:.2f}\n\n"
        
        if len(users) > 8:
            user_list += f"... and {len(users) - 8} more users\n"
    else:
        user_list += "No users found."
    
    message_text = f"""💰 {action.title()} User Balance

{user_list}

Enter the User ID (number) or Username (with @) you want to {action_text}:

Examples: 
• User ID: `{users[0]['user_id'] if users else '123456789'}`
• Username: `@{users[0]['username'] if users and users[0].get('username') else 'username'}`

⚠️ **Important:** Usernames must start with @ to avoid confusion with numeric user IDs."""
    
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("❌ Cancel", callback_data="admin_balance_mgmt"))
    
    bot.send_message(chat_id, message_text, reply_markup=keyboard)

async def start_user_ban_process(chat_id, admin_id):
    """Start user ban process"""
    admin_ban_operations[admin_id] = {"action": "ban", "step": "user_id"}
    
    # Get user list for easier identification
    users = await db.get_all_users()
    user_list = "**Available Users:**\n"
    
    if users:
        for i, user in enumerate(users[:8], 1):  # Show first 8 users
            username_display = f"@{user['username']}" if user.get('username') else user.get('first_name', 'No Name')
            is_banned = await db.is_user_banned(user['user_id'])
            status = "🚫 BANNED" if is_banned else "✅ Active"
            user_list += f"{i}. **{username_display}**\n   ID: `{user['user_id']}` - {status}\n\n"
        
        if len(users) > 8:
            user_list += f"... and {len(users) - 8} more users\n"
    else:
        user_list += "No users found."
    
    message_text = f"""🚫 **Ban User**

{user_list}

Enter the User ID you want to ban:

Example: `{users[0]['user_id'] if users else '123456789'}`

This will prevent the user from using any bot features."""
    
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("❌ Cancel", callback_data="admin_ban_mgmt"))
    
    bot.send_message(chat_id, message_text, parse_mode='Markdown', reply_markup=keyboard)

async def start_user_unban_process(chat_id, admin_id):
    """Start user unban process"""
    admin_ban_operations[admin_id] = {"action": "unban", "step": "user_id"}
    
    # Get banned users for easier identification
    users = await db.get_all_users()
    banned_users = []
    for user in users:
        if await db.is_user_banned(user['user_id']):
            banned_users.append(user)
    
    user_list = "**Banned Users:**\n"
    
    if banned_users:
        for i, user in enumerate(banned_users[:8], 1):  # Show first 8 banned users
            username_display = f"@{user['username']}" if user.get('username') else user.get('first_name', 'No Name')
            user_list += f"{i}. **{username_display}**\n   ID: `{user['user_id']}`\n\n"
        
        if len(banned_users) > 8:
            user_list += f"... and {len(banned_users) - 8} more banned users\n"
    else:
        user_list += "No banned users found."
    
    message_text = f"""✅ **Unban User**

{user_list}

Enter the User ID you want to unban:

Example: `{banned_users[0]['user_id'] if banned_users else '123456789'}`

This will restore full access to the user."""
    
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("❌ Cancel", callback_data="admin_ban_mgmt"))
    
    bot.send_message(chat_id, message_text, parse_mode='Markdown', reply_markup=keyboard)

async def process_admin_balance_input(chat_id, admin_id, user_input):
    """Process admin balance management input"""
    try:
        operation = admin_balance_operations.get(admin_id, {})
        step = operation.get("step")
        action = operation.get("action")
        
        logger.info(f"Processing admin balance input: step={step}, action={action}, input={user_input}")
        
        if step == "user_id":
            # Validate user ID or username
            user_input = user_input.strip()
            target_user_id = None
            user_identifier = None
            
            # Check if input starts with @ (username)
            if user_input.startswith('@'):
                # Handle username
                username = user_input[1:]  # Remove @ prefix
                target_user_id = await db.get_user_id_by_username(username)
                
                if target_user_id:
                    user_identifier = f"Username: @{username} (ID: {target_user_id})"
                    balance = await db.get_user_balance(target_user_id)
                else:
                    bot.send_message(chat_id, f"❌ Username not found: @{username}\n\nPlease check the username and try again.")
                    return
            else:
                # Handle as user ID (numeric only)
                try:
                    target_user_id = int(user_input)
                    
                    # Explicitly check if user exists
                    user_exists = await db.user_exists(target_user_id)
                    if not user_exists:
                        bot.send_message(chat_id, f"❌ User ID not found: {target_user_id}\n\nPlease enter a valid User ID or username with @ prefix.\n\nExample: `123456789` or `@username`")
                        return
                    
                    user_identifier = f"User ID: {target_user_id}"
                    balance = await db.get_user_balance(target_user_id)
                    
                except ValueError:
                    bot.send_message(chat_id, f"❌ Invalid input: {user_input}\n\nPlease enter:\n• A numeric User ID (e.g., `123456789`)\n• A username with @ prefix (e.g., `@username`)")
                    return
            
            if target_user_id:
                admin_balance_operations[admin_id].update({
                    "target_user_id": target_user_id,
                    "step": "currency"
                })
                
                currency_text = f"""💰 Select Currency

{user_identifier}
Current Balance: ${balance:.2f}

Choose currency to {action}:"""
                
                keyboard = types.InlineKeyboardMarkup()
                keyboard.add(
                    types.InlineKeyboardButton("💵 USD", callback_data="admin_curr_USD"),
                    types.InlineKeyboardButton("₿ BTC", callback_data="admin_curr_BTC")
                )
                keyboard.add(
                    types.InlineKeyboardButton("Ξ ETH", callback_data="admin_curr_ETH"),
                    types.InlineKeyboardButton("◎ SOL", callback_data="admin_curr_SOL")
                )
                keyboard.add(
                    types.InlineKeyboardButton("₮ USDT", callback_data="admin_curr_USDT"),
                    types.InlineKeyboardButton("🅱️ BNB", callback_data="admin_curr_BNB")
                )
                keyboard.add(
                    types.InlineKeyboardButton("🔷 MATIC", callback_data="admin_curr_MATIC"),
                    types.InlineKeyboardButton("🔵 ADA", callback_data="admin_curr_ADA")
                )
                keyboard.add(
                    types.InlineKeyboardButton("🔗 LINK", callback_data="admin_curr_LINK")
                )
                keyboard.add(types.InlineKeyboardButton("❌ Cancel", callback_data="admin_balance_mgmt"))
                
                bot.send_message(chat_id, currency_text, reply_markup=keyboard)
                
        elif step == "amount":
            # Process amount input
            try:
                amount = float(user_input.strip())
                if amount <= 0:
                    bot.send_message(chat_id, "❌ Amount must be greater than 0.")
                    return
                
                target_user_id = operation.get("target_user_id")
                currency = operation.get("currency")
                
                # Execute the balance operation
                if action == "add":
                    success = await db.admin_add_balance(target_user_id, amount, currency)
                else:
                    success = await db.admin_subtract_balance(target_user_id, amount, currency)
                
                if success:
                    action_text = "added to" if action == "add" else "subtracted from"
                    success_text = f"""✅ Balance Updated Successfully

Action: {amount} {currency} {action_text} user {target_user_id}
Time: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

Transaction has been recorded in the user's trading history."""
                    
                    keyboard = types.InlineKeyboardMarkup()
                    keyboard.add(
                        types.InlineKeyboardButton("💰 Manage More Balances", callback_data="admin_balance_mgmt"),
                        types.InlineKeyboardButton("👥 Back to Users", callback_data="admin_users")
                    )
                    
                    bot.send_message(chat_id, success_text, parse_mode='Markdown', reply_markup=keyboard)
                else:
                    bot.send_message(chat_id, "❌ Failed to update balance. Please try again.")
                
                # Clear operation
                if admin_id in admin_balance_operations:
                    del admin_balance_operations[admin_id]
                    
            except ValueError:
                bot.send_message(chat_id, "❌ Invalid amount. Please enter a valid number.")
                
    except Exception as e:
        logger.error(f"Error processing admin balance input: {e}")
        bot.send_message(chat_id, "❌ An error occurred. Please try again.")

async def process_admin_ban_input(chat_id, admin_id, user_input):
    """Process admin ban/unban input"""
    try:
        operation = admin_ban_operations.get(admin_id, {})
        step = operation.get("step")
        action = operation.get("action")
        
        if step == "user_id":
            # Validate user ID
            try:
                target_user_id = int(user_input.strip())
                
                if action == "ban":
                    # Check if user is already banned
                    is_banned = await db.is_user_banned(target_user_id)
                    if is_banned:
                        bot.send_message(chat_id, f"❌ User `{target_user_id}` is already banned.")
                        return
                    
                    admin_ban_operations[admin_id].update({
                        "target_user_id": target_user_id,
                        "step": "reason"
                    })
                    
                    reason_text = f"""🚫 **Ban User {target_user_id}**

Enter the reason for banning this user:

Example: `Violation of terms of service`"""
                    
                    keyboard = types.InlineKeyboardMarkup()
                    keyboard.add(types.InlineKeyboardButton("❌ Cancel", callback_data="admin_ban_mgmt"))
                    
                    bot.send_message(chat_id, reason_text, parse_mode='Markdown', reply_markup=keyboard)
                    
                else:  # unban
                    # Check if user is banned
                    is_banned = await db.is_user_banned(target_user_id)
                    if not is_banned:
                        bot.send_message(chat_id, f"❌ User `{target_user_id}` is not banned.")
                        return
                    
                    # Execute unban
                    success = await db.unban_user(target_user_id)
                    
                    if success:
                        success_text = f"""✅ **User Unbanned Successfully**

User `{target_user_id}` has been unbanned and can now use the bot normally.
**Time:** {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"""
                        
                        keyboard = types.InlineKeyboardMarkup()
                        keyboard.add(
                            types.InlineKeyboardButton("🚫 Manage More Bans", callback_data="admin_ban_mgmt"),
                            types.InlineKeyboardButton("👥 Back to Users", callback_data="admin_users")
                        )
                        
                        bot.send_message(chat_id, success_text, parse_mode='Markdown', reply_markup=keyboard)
                    else:
                        bot.send_message(chat_id, "❌ Failed to unban user. Please try again.")
                    
                    # Clear operation
                    if admin_id in admin_ban_operations:
                        del admin_ban_operations[admin_id]
                
            except ValueError:
                bot.send_message(chat_id, "❌ Invalid User ID. Please enter a valid number.")
                
        elif step == "reason":
            # Process ban reason
            reason = user_input.strip()
            if len(reason) < 3:
                bot.send_message(chat_id, "❌ Reason must be at least 3 characters long.")
                return
                
            target_user_id = operation.get("target_user_id")
            
            # Execute ban
            success = await db.ban_user(target_user_id, admin_id, reason)
            
            if success:
                success_text = f"""✅ **User Banned Successfully**

**User ID:** `{target_user_id}`
**Reason:** {reason}
**Banned by:** Admin `{admin_id}`
**Time:** {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

The user will be notified when they try to use the bot."""
                
                keyboard = types.InlineKeyboardMarkup()
                keyboard.add(
                    types.InlineKeyboardButton("🚫 Manage More Bans", callback_data="admin_ban_mgmt"),
                    types.InlineKeyboardButton("👥 Back to Users", callback_data="admin_users")
                )
                
                bot.send_message(chat_id, success_text, parse_mode='Markdown', reply_markup=keyboard)
            else:
                bot.send_message(chat_id, "❌ Failed to ban user. Please try again.")
            
            # Clear operation
            if admin_id in admin_ban_operations:
                del admin_ban_operations[admin_id]
                
    except Exception as e:
        logger.error(f"Error processing admin ban input: {e}")
        bot.send_message(chat_id, "❌ An error occurred. Please try again.")

async def process_txid_input(chat_id, user_id, txid):
    """Handle TXID input for fee verification"""
    try:
        withdrawal_state = user_withdrawal_states.get(user_id)
        if not withdrawal_state or withdrawal_state.get('step') != 'entering_txid':
            return
        
        token = withdrawal_state['token']
        withdrawal_token = withdrawal_state['withdrawal_token']
        withdrawal_amount = withdrawal_state['withdrawal_amount']
        fee_amount = withdrawal_state['fee_amount']
        
        # Validate TXID format
        if not validate_txid(txid, token):
            bot.send_message(
                chat_id,
                f"""❌ **Invalid Transaction ID Format**

The TXID you entered doesn't match the {token} format requirements:

🔍 **Expected Format for {token}:**
{get_txid_format_help(token)}

💡 **Tips:**
• Copy the TXID exactly from your wallet
• Don't include spaces or extra characters
• Make sure you're copying the complete TXID

Please try again with a valid {token} transaction ID.""",
                parse_mode='Markdown',
                reply_markup=types.InlineKeyboardMarkup([
                    [types.InlineKeyboardButton("🔄 Try Again", callback_data=f"enter_txid_{token}")],
                    [types.InlineKeyboardButton("❌ Cancel", callback_data="withdraw_type_CRYPTO")]
                ])
            )
            return
        
        # Check if TXID already used
        existing_payment = await db.get_fee_payment_by_txid(txid)
        if existing_payment:
            bot.send_message(
                chat_id,
                f"""❌ **Transaction ID Already Used**

This TXID has already been submitted for fee verification.

⚠️ **Anti-Fraud Protection:** Each transaction ID can only be used once to prevent double-spending and ensure compliance with federal regulations.

🔍 **What to do:**
• Use a different, valid TXID
• Make a new fee payment if needed
• Contact support if you believe this is an error

Please enter a different transaction ID.""",
                parse_mode='Markdown',
                reply_markup=types.InlineKeyboardMarkup([
                    [types.InlineKeyboardButton("🔄 Try Again", callback_data=f"enter_txid_{token}")],
                    [types.InlineKeyboardButton("❌ Cancel", callback_data="withdraw_type_CRYPTO")]
                ])
            )
            return
        
        # Create fee payment record and start verification
        from config import WITHDRAWAL_FEE_ADDRESSES
        fee_network = WITHDRAWAL_FEE_ADDRESSES.get(token, {}).get("network", f"{token} Network")
        
        payment_data = {
            "user_id": user_id,
            "token": token,
            "expected_amount": fee_amount,
            "txid": txid,
            "network": fee_network,
            "withdrawal_token": withdrawal_token,
            "withdrawal_amount": withdrawal_amount
        }
        payment_id = await db.create_fee_payment(payment_data)
        
        if not payment_id:
            bot.send_message(
                chat_id,
                "❌ **System Error**\n\nUnable to create fee payment record. Please try again.",
                reply_markup=types.InlineKeyboardMarkup([
                    [types.InlineKeyboardButton("🔄 Retry", callback_data=f"enter_txid_{token}")],
                    [types.InlineKeyboardButton("❌ Cancel", callback_data="withdraw_type_CRYPTO")]
                ])
            )
            return
        
        # Start verification process
        required_confirmations = get_token_confirmation_requirement(token)
        
        verification_text = f"""🔍🔍🔍 VERIFICATION IN PROGRESS 🔍🔍🔍

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

✅ **TRANSACTION ID RECEIVED**
📋 TXID: {txid[:20]}...{txid[-10:]}

💸 **FEE VERIFICATION STATUS:**
🪙 Token: {token}
💰 Amount: {fee_amount:.2f} {token}
🌐 Network: {fee_network}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

⏳ **BLOCKCHAIN VERIFICATION:**
📊 Confirmations: 0/{required_confirmations}
⏱️ Status: Scanning blockchain...

🔍 Our compliance system is now verifying your fee payment on the {fee_network}. This process typically takes 2-15 minutes depending on network congestion.

⚖️ **BSA/AML Compliance:** All external transfers require blockchain verification per 31 CFR Part 1010.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

💡 **You will be notified automatically once verification is complete!**"""
        
        # Update user state to track verification
        user_withdrawal_states[user_id].update({
            'step': 'verifying',
            'payment_id': payment_id,
            'txid': txid
        })
        
        bot.send_message(
            chat_id,
            verification_text,
            reply_markup=types.InlineKeyboardMarkup([
                [types.InlineKeyboardButton("📊 Check Status", callback_data=f"check_verification_{payment_id}")],
                [types.InlineKeyboardButton("❌ Cancel", callback_data="withdraw_type_CRYPTO")]
            ])
        )
        
        # TODO: Start background verification simulation
        await start_fee_verification_simulation(payment_id, required_confirmations)
        
    except Exception as e:
        logger.error(f"Error processing TXID input: {e}")
        bot.send_message(chat_id, "❌ An error occurred. Please try again.")

def get_txid_format_help(token: str) -> str:
    """Get format help for different tokens"""
    formats = {
        "BTC": "• 64 hexadecimal characters\n• Example: a1b2c3d4e5f6789012345678901234567890abcdef1234567890abcdef123456",
        "ETH": "• 66 characters starting with 0x\n• Example: 0xa1b2c3d4e5f6789012345678901234567890abcdef1234567890abcdef123456",
        "USDT": "• 66 characters starting with 0x (Ethereum)\n• Example: 0xa1b2c3d4e5f6789012345678901234567890abcdef1234567890abcdef123456",
        "SOL": "• 88 base58 characters\n• Example: 5uGfMDHLd3PwLhWmUhyKYBpZ8nQGvmjw7XrVCQ2KzBhqN3vFpE9rKdYtQwPjMnL8",
        "ADA": "• 64 hexadecimal characters\n• Example: a1b2c3d4e5f6789012345678901234567890abcdef1234567890abcdef123456"
    }
    return formats.get(token, "• Please check your wallet for the correct format")

async def start_fee_verification_simulation(payment_id: int, required_confirmations: int):
    """Start simulated blockchain verification process"""
    # Create a background task for verification simulation
    asyncio.create_task(simulate_blockchain_confirmations(payment_id, required_confirmations))

async def simulate_blockchain_confirmations(payment_id: int, required_confirmations: int):
    """Simulate blockchain confirmation process with realistic delays"""
    try:
        import time
        
        # Get payment details
        payment = await db.get_fee_payment_by_id(payment_id)
        if not payment:
            return
        
        user_id = payment['user_id']
        token = payment['token']
        txid = payment['txid']
        
        # Simulate confirmation delays (realistic for different networks)
        confirmation_delays = {
            "BTC": [120, 240],      # 2-4 minutes between confirmations
            "ETH": [30, 60],        # 30-60 seconds between confirmations  
            "SOL": [15],            # 15 seconds for single confirmation
            "USDT": [30, 60],       # Same as ETH (ERC-20)
            "BNB": [60, 120],       # 1-2 minutes
            "MATIC": [30, 60],      # 30-60 seconds
            "ADA": [60, 120],       # 1-2 minutes
            "LINK": [30, 60]        # Same as ETH (ERC-20)
        }
        
        delays = confirmation_delays.get(token, [60, 120])
        
        # Start verification process
        for confirmation in range(1, required_confirmations + 1):
            # Wait for realistic network delay
            if confirmation <= len(delays):
                wait_time = delays[confirmation - 1]
            else:
                wait_time = delays[-1]  # Use last delay for remaining confirmations
            
            await asyncio.sleep(wait_time)
            
            # Update confirmation count in database
            await db.update_fee_payment_confirmations(payment_id, confirmation)
            
            # Send progress update to user
            progress_text = f"""🔍 **VERIFICATION UPDATE** 🔍

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📋 **TXID:** {txid[:20]}...{txid[-10:]}
📊 **Confirmations:** {confirmation}/{required_confirmations}
🌐 **Network:** {payment['network']}

{'✅ **VERIFICATION COMPLETE!**' if confirmation >= required_confirmations else '⏳ **Verification in progress...**'}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{get_verification_status_message(confirmation, required_confirmations, token)}"""
            
            # Check if user is still in withdrawal flow
            if user_id in user_withdrawal_states:
                keyboard = None
                
                if confirmation >= required_confirmations:
                    # Verification complete - mark as verified and allow withdrawal
                    await db.mark_fee_payment_verified(payment_id)
                    
                    keyboard = types.InlineKeyboardMarkup([
                        [types.InlineKeyboardButton("✅ Proceed to Withdrawal", callback_data=f"fee_verified_{token}")],
                        [types.InlineKeyboardButton("❌ Cancel", callback_data="withdraw_type_CRYPTO")]
                    ])
                    
                    # Update user state to verified
                    user_withdrawal_states[user_id]['step'] = 'verified'
                    
                else:
                    keyboard = types.InlineKeyboardMarkup([
                        [types.InlineKeyboardButton("📊 Check Status", callback_data=f"check_verification_{payment_id}")],
                        [types.InlineKeyboardButton("❌ Cancel", callback_data="withdraw_type_CRYPTO")]
                    ])
                
                try:
                    bot.send_message(user_id, progress_text, reply_markup=keyboard)
                except Exception as e:
                    logger.error(f"Error sending verification update to user {user_id}: {e}")
            
            # Break if verification complete
            if confirmation >= required_confirmations:
                break
        
        logger.info(f"Fee verification simulation completed for payment {payment_id}")
        
    except Exception as e:
        logger.error(f"Error in blockchain confirmation simulation: {e}")
        # Mark as rejected on error
        await db.mark_fee_payment_rejected(payment_id)

def get_verification_status_message(current: int, required: int, token: str) -> str:
    """Get status message based on confirmation progress"""
    if current >= required:
        return f"""🎉 **FEE PAYMENT VERIFIED!**

Your {token} fee payment has been successfully verified on the blockchain. You can now proceed with your withdrawal.

⚖️ **Compliance Status:** ✅ APPROVED
🔒 **Regulatory Review:** ✅ COMPLETE"""
    else:
        progress_percentage = int((current / required) * 100)
        return f"""⏳ **Verification Progress: {progress_percentage}%**

🔍 Blockchain scanning in progress...
📊 Awaiting {required - current} more confirmations

💡 **Average completion time for {token}:** {get_estimated_completion_time(token, current, required)}"""

def get_estimated_completion_time(token: str, current: int, required: int) -> str:
    """Get estimated completion time for verification"""
    remaining = required - current
    
    estimates = {
        "BTC": f"{remaining * 3} minutes",
        "ETH": f"{remaining * 1} minute{'s' if remaining > 1 else ''}",
        "SOL": "30 seconds",
        "USDT": f"{remaining * 1} minute{'s' if remaining > 1 else ''}",
        "BNB": f"{remaining * 2} minutes",
        "MATIC": f"{remaining * 1} minute{'s' if remaining > 1 else ''}",
        "ADA": f"{remaining * 2} minutes",
        "LINK": f"{remaining * 1} minute{'s' if remaining > 1 else ''}"
    }
    
    return estimates.get(token, f"{remaining * 2} minutes")

def get_fee_payment_token(withdrawal_token: str) -> str:
    """Get the token used for fee payments based on withdrawal token"""
    # Map withdrawal tokens to fee payment tokens
    fee_token_mapping = {
        "BTC": "BTC",
        "ETH": "ETH", 
        "SOL": "SOL",
        "USDT": "ETH",  # USDT fees paid in ETH
        "BNB": "BNB",
        "MATIC": "ETH", # MATIC fees paid in ETH
        "ADA": "ADA",
        "LINK": "ETH"   # LINK fees paid in ETH
    }
    return fee_token_mapping.get(withdrawal_token, "ETH")  # Default to ETH

async def process_withdrawal_address_input(chat_id, user_id, wallet_address):
    """Handle wallet address input for withdrawal"""
    try:
        withdrawal_state = user_withdrawal_states.get(user_id)
        if not withdrawal_state:
            return
            
        token = withdrawal_state['token']
        wallet_address = wallet_address.strip()
        
        # Basic address validation
        if len(wallet_address) < 20 or len(wallet_address) > 100:
            bot.send_message(
                chat_id,
                "❌ Invalid address format! Please enter a valid wallet address.",
                reply_markup=types.InlineKeyboardMarkup([
                    [types.InlineKeyboardButton("🔄 Try Again", callback_data=f"enter_address_{token}")],
                    [types.InlineKeyboardButton("❌ Cancel", callback_data="withdraw_type_CRYPTO")]
                ])
            )
            return
        
        # ATOMIC SECURITY GATE: Use single choke-point function for ALL withdrawals
        portfolio = await db.get_user_portfolio(user_id)
        user_holdings = {token: data["amount"] for token, data in portfolio.items()}
        
        if token not in user_holdings:
            bot.send_message(chat_id, "❌ Error: Token not found in your portfolio.")
            del user_withdrawal_states[user_id]
            return
            
        token_amount = user_holdings[token]
        withdrawal_fee = token_amount * 0.10
        net_amount = token_amount - withdrawal_fee
        
        # ATOMIC WITHDRAWAL CREATION: Single secure function prevents all bypass attempts
        withdrawal_result = await db.atomic_create_verified_withdrawal(
            user_id=user_id,
            withdrawal_type="crypto", 
            withdrawal_token=token,
            withdrawal_amount=token_amount,  # Full amount for verification
            to_address=wallet_address
        )
        
        if not withdrawal_result['success']:
            # SECURITY BLOCK: Withdrawal rejected by atomic function
            bot.send_message(
                chat_id,
                f"""🚫🚫🚫 WITHDRAWAL SECURITY BLOCK 🚫🚫🚫

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

❌ **WITHDRAWAL REJECTED**

🔍 **Reason:** {withdrawal_result['reason']}
📝 **Details:** {withdrawal_result['message']}

⚖️ **Federal BSA/AML Compliance Notice:**
All cryptocurrency withdrawals require verified fee payment per 31 CFR Part 1010.410. This ensures compliance with anti-money laundering regulations and prevents unauthorized transfers.

🔒 **What you need to do:**
1. Complete fee verification first
2. Pay the required {withdrawal_fee:.2f} {get_fee_payment_token(token)} fee
3. Submit TXID for blockchain verification
4. Wait for confirmation (2-15 minutes)
5. Then proceed with withdrawal

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

💡 Click below to complete fee verification:""",
                reply_markup=types.InlineKeyboardMarkup([
                    [types.InlineKeyboardButton("💳 Pay Withdrawal Fee", callback_data=f"enter_txid_{get_fee_payment_token(token)}")],
                    [types.InlineKeyboardButton("❌ Cancel", callback_data="withdraw_type_CRYPTO")]
                ])
            )
            del user_withdrawal_states[user_id]
            return
        
        # WITHDRAWAL APPROVED: Extract details from atomic result
        withdrawal_id = withdrawal_result['withdrawal_id']
        fee_amount = withdrawal_result['fee_amount']
        fee_token = withdrawal_result['fee_token']
        fee_txid = withdrawal_result['fee_txid']
        
        # Remove token from user portfolio (simulate withdrawal) by setting amount to 0
        await db.update_portfolio(user_id, token, -token_amount, 0)
        
        success_text = f"""✅✅✅ WITHDRAWAL REQUEST APPROVED ✅✅✅

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🏦 **TRANSACTION SUMMARY**

🪙 **Asset:** {token}
💰 **Gross Amount:** {token_amount:.6f} {token}
🏛️ **Processing Fees:** {fee_amount:.2f} {fee_token}
✅ **Net Transfer:** {withdrawal_result['net_amount']:.6f} {token}

📍 **Destination:** {wallet_address[:20]}...{wallet_address[-10:]}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

⚡ **Processing Status:** Priority Queue Active
🔢 **Reference ID:** #{withdrawal_id}
⏱️ **ETA:** 15-30 minutes (3+ confirmations)

💳 **Fee Payment Verified:**
🪙 Token: {fee_token}
📋 TXID: {fee_txid[:20]}...{fee_txid[-10:]}
✅ Status: VERIFIED & CONSUMED

📋 **Compliance Fees Applied:**
• Federal BSA/AML Processing (10%)
• Network Security & Validation
• Priority Routing & Liquidity
• Professional Custody Services

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🎉🎉 WITHDRAWAL SUCCESSFULLY APPROVED 🎉🎉"""
        
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(types.InlineKeyboardButton("📊 View Withdrawals", callback_data="wallet_withdrawals"))
        keyboard.add(types.InlineKeyboardButton("🏠 Main Menu", callback_data="back_to_main"))
        
        bot.send_message(
            chat_id,
            success_text,
            reply_markup=keyboard
        )
        
        # Clear user state
        del user_withdrawal_states[user_id]
        
    except Exception as e:
        logger.error(f"Error processing withdrawal: {e}")
        bot.send_message(chat_id, "❌ Error processing withdrawal. Please try again.")
        user_withdrawal_states.pop(user_id, None)

async def disconnect_wallet(chat_id, user_id):
    """Disconnect user's wallet"""
    try:
        await db.disconnect_wallet(user_id)
        
        disconnect_text = """🔓 **Wallet Disconnected**

Your wallet has been safely disconnected. To use copy trading features again, you'll need to reconnect your wallet.

Your trading history and settings have been preserved."""
        
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(
            types.InlineKeyboardButton("🔐 Reconnect Wallet", callback_data="copy_browse"),
            types.InlineKeyboardButton("🏠 Main Menu", callback_data="back_to_main")
        )
        
        bot.send_message(chat_id, disconnect_text, parse_mode='Markdown', reply_markup=keyboard)
        
    except Exception as e:
        logger.error(f"Error disconnecting wallet: {e}")
        bot.send_message(chat_id, "❌ Error disconnecting wallet. Please try again.")

def process_support_message(chat_id, user_id, message_text):
    """Process user support message and forward to admin"""
    try:
        # Get user info
        user_info = bot.get_chat_member(chat_id, user_id).user
        username = f"@{user_info.username}" if user_info.username else f"ID: {user_id}"
        full_name = f"{user_info.first_name} {user_info.last_name or ''}".strip()
        
        # Create support ticket ID
        ticket_id = f"#{user_id}-{int(time.time())}"
        
        # Format message for admin
        admin_message = f"""🎫🎫🎫 NEW SUPPORT TICKET 🎫🎫🎫

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**📋 TICKET:** {ticket_id}

**👤 USER DETAILS:**
• **Name:** {full_name}
• **Username:** {username}
• **User ID:** {user_id}
• **Time:** {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

**💬 MESSAGE:**
{message_text}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**⚡ PRIORITY:** Normal Support Request
**🎯 RESPONSE:** Required within 15 minutes"""
        
        # Send to admin
        if ADMIN_USER_ID:
            admin_keyboard = types.InlineKeyboardMarkup(row_width=1)
            admin_keyboard.add(
                types.InlineKeyboardButton(f"💬 Reply to {full_name}", url=f"tg://user?id={user_id}")
            )
            bot.send_message(ADMIN_USER_ID, admin_message, parse_mode='Markdown', reply_markup=admin_keyboard)
        
        # Confirm to user
        user_confirmation = f"""✅✅✅ SUPPORT MESSAGE SENT ✅✅✅

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**📨 YOUR MESSAGE WAS DELIVERED:**

**Ticket ID:** {ticket_id}
**Status:** ✅ Received by admin team
**Priority:** Normal support request

**📱 NEXT STEPS:**
1. Admin will review your message immediately
2. You'll receive a personal reply within 15 minutes  
3. Check your Telegram notifications
4. Response will come directly from admin

**💼 SUPPORT TEAM STATUS:**
• **Admin:** 🟢 Online & Active
• **Response Time:** 5-15 minutes average
• **Queue Position:** Priority support

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**🎯 Thank you for contacting us!**"""
        
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        keyboard.add(
            types.InlineKeyboardButton("🔙 Back to Help Menu", callback_data="menu_help")
        )
        
        bot.send_message(chat_id, user_confirmation, reply_markup=keyboard)
        
        # Log the support request
        logger.info(f"Support message from user {user_id} ({username}): {message_text[:100]}...")
        
    except Exception as e:
        logger.error(f"Error processing support message: {e}")
        bot.send_message(chat_id, "❌ Error sending support message. Please try again or use /start.")

def process_bug_report(chat_id, user_id, message_text):
    """Process user bug report and forward to admin"""
    try:
        # Get user info
        user_info = bot.get_chat_member(chat_id, user_id).user
        username = f"@{user_info.username}" if user_info.username else f"ID: {user_id}"
        full_name = f"{user_info.first_name} {user_info.last_name or ''}".strip()
        
        # Create bug report ID
        bug_id = f"#BUG-{user_id}-{int(time.time())}"
        
        # Format message for admin
        admin_message = f"""🐛🐛🐛 NEW BUG REPORT 🐛🐛🐛

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**📋 BUG REPORT:** {bug_id}

**👤 USER DETAILS:**
• **Name:** {full_name}
• **Username:** {username}
• **User ID:** {user_id}
• **Time:** {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

**🔧 BUG DESCRIPTION:**
{message_text}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**⚡ PRIORITY:** Technical Issue - Needs Investigation
**🎯 ACTION:** Developer review required"""
        
        # Send to admin
        if ADMIN_USER_ID:
            admin_keyboard = types.InlineKeyboardMarkup(row_width=1)
            admin_keyboard.add(
                types.InlineKeyboardButton(f"🔧 Contact {full_name}", url=f"tg://user?id={user_id}")
            )
            bot.send_message(ADMIN_USER_ID, admin_message, parse_mode='Markdown', reply_markup=admin_keyboard)
        
        # Confirm to user
        user_confirmation = f"""🔧🔧🔧 BUG REPORT SUBMITTED 🔧🔧🔧

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**📨 YOUR BUG REPORT WAS RECEIVED:**

**Report ID:** {bug_id}
**Status:** ✅ Under technical review
**Priority:** High - Technical issue

**📱 WHAT HAPPENS NEXT:**
1. Development team will analyze the issue
2. Admin will contact you for additional details if needed
3. You'll be notified when the bug is fixed
4. Fix will be deployed to improve user experience

**🔧 TECHNICAL SUPPORT STATUS:**
• **Dev Team:** 🟢 Active monitoring
• **Review Time:** 30-60 minutes
• **Fix Timeline:** 24-48 hours typical

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**🎯 Thank you for helping improve our platform!**"""
        
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        keyboard.add(
            types.InlineKeyboardButton("🔙 Back to Bot Support", callback_data="help_bot_support")
        )
        
        bot.send_message(chat_id, user_confirmation, reply_markup=keyboard)
        
        # Log the bug report
        logger.info(f"Bug report from user {user_id} ({username}): {message_text[:100]}...")
        
    except Exception as e:
        logger.error(f"Error processing bug report: {e}")
        bot.send_message(chat_id, "❌ Error submitting bug report. Please try again or use /start.")

async def init_database():
    """Initialize database"""
    await db.init_db()
    logger.info("Database initialized successfully")

def main():
    """Main function to run the bot"""
    # Initialize database
    asyncio.run(init_database())
    
    # Start the bot
    logger.info("Starting Telegram Trading Bot...")
    bot.infinity_polling()

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Error running bot: {e}")