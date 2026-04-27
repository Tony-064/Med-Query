import os
import logging
import requests as http_requests
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from google import genai
from dotenv import load_dotenv
from datetime import datetime

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.DEBUG)

# Create the app
app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", "dev-secret-key-change-in-production")
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Supabase REST API helpers (using requests to avoid httpx version conflicts)
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set in environment variables")

def _sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }

def sb_select(table, filters=None, order=None):
    """SELECT rows from a Supabase table."""
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    params = {"select": "*"}
    if filters:
        params.update(filters)
    if order:
        params["order"] = order
    resp = http_requests.get(url, headers=_sb_headers(), params=params)
    resp.raise_for_status()
    return resp.json()

def sb_insert(table, data):
    """INSERT a row into a Supabase table."""
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    resp = http_requests.post(url, headers=_sb_headers(), json=data)
    resp.raise_for_status()
    return resp.json()

# Initialize login manager
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access the chat interface.'

# Configure Google AI (new google.genai SDK)
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
genai_client = genai.Client(api_key=GOOGLE_API_KEY) if GOOGLE_API_KEY else None

class User:
    def __init__(self, id, username, email, password_hash):
        self.id = id
        self.username = username
        self.email = email
        self.password_hash = password_hash
        self.is_authenticated = True
        self.is_active = True
        self.is_anonymous = False

    def get_id(self):
        return str(self.id)

@login_manager.user_loader
def load_user(user_id):
    try:
        rows = sb_select('users', filters={'id': f'eq.{int(user_id)}'} )
        if rows:
            user_data = rows[0]
            return User(
                id=user_data['id'],
                username=user_data['username'],
                email=user_data['email'],
                password_hash=user_data['password_hash']
            )
    except Exception as e:
        logging.error(f"Error loading user: {e}")
    return None

@app.route('/')
def home():
    return render_template('home.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']
        password = request.form['password']
        
        try:
            # Check if user exists
            if sb_select('users', filters={'username': f'eq.{username}'}):
                flash('Username already exists. Please choose a different one.', 'error')
                return render_template('register.html')

            if sb_select('users', filters={'email': f'eq.{email}'}):
                flash('Email already registered. Please use a different email.', 'error')
                return render_template('register.html')

            # Create new user
            user_data = {
                'username': username,
                'email': email,
                'password_hash': generate_password_hash(password),
                'created_at': datetime.utcnow().isoformat()
            }
            sb_insert('users', user_data)
            flash('Registration successful! Please log in.', 'success')
            return redirect(url_for('login'))

        except Exception as e:
            flash('Registration failed. Please try again.', 'error')
            logging.error(f"Registration error: {e}")
    
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        try:
            # Check for admin login
            if username == 'admin' and password == 'admin':
                session['is_admin'] = True
                flash('Admin login successful!', 'success')
                return redirect(url_for('admin'))

            # Regular user login
            rows = sb_select('users', filters={'username': f'eq.{username}'})
            if rows:
                user_data = rows[0]
                if check_password_hash(user_data['password_hash'], password):
                    user = User(
                        id=user_data['id'],
                        username=user_data['username'],
                        email=user_data['email'],
                        password_hash=user_data['password_hash']
                    )
                    login_user(user)
                    session['chat_stage'] = 'name'
                    flash('Login successful!', 'success')
                    return redirect(url_for('chat'))

            flash('Invalid username or password.', 'error')

        except Exception as e:
            flash('Login failed. Please try again.', 'error')
            logging.error(f"Login error: {e}")
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))

@app.route('/chat')
@login_required
def chat():
    return render_template('chat.html')

@app.route('/admin')
def admin():
    if not session.get('is_admin'):
        flash('Access denied. Admin privileges required.', 'error')
        return redirect(url_for('login'))
    
    try:
        users = sb_select('users')
        messages = sb_select('chat_messages')
        return render_template('admin.html', users=users, messages=messages)
    except Exception as e:
        flash('Error loading admin data.', 'error')
        logging.error(f"Admin page error: {e}")
        return redirect(url_for('login'))

@app.route('/api/chat', methods=['POST'])
@login_required
def chat_api():
    try:
        data = request.get_json()
        user_message = data.get('message', '').strip()
        
        if not user_message:
            return jsonify({'error': 'Message cannot be empty'}), 400
        
        # Get current chat stage
        chat_stage = session.get('chat_stage', 'name')
        
        # Save user message
        message_data = {
            'user_id': current_user.id,
            'message': user_message,
            'is_bot': False,
            'chat_stage': chat_stage,
            'timestamp': datetime.utcnow().isoformat()
        }
        sb_insert('chat_messages', message_data)

        # Process based on chat stage
        if chat_stage == 'name':
            session['user_name'] = user_message
            session['chat_stage'] = 'age'
            bot_response = f"Nice to meet you, {user_message}! What is your age?"

        elif chat_stage == 'age':
            try:
                age = int(user_message)
                session['user_age'] = age
                session['chat_stage'] = 'medical_history'
                bot_response = "Thank you. Do you have any past medical conditions? Please describe them or say 'none' if you don't have any."
            except ValueError:
                bot_response = "Please enter a valid age (numbers only)."

        elif chat_stage == 'medical_history':
            session['medical_history'] = user_message
            session['chat_stage'] = 'free_form'
            bot_response = "Thank you for providing your information. Now I can help you with any medical questions you might have. What would you like to know?"

        else:  # free_form stage - use AI
            bot_response = get_ai_response(user_message)

        # Save bot response
        bot_message_data = {
            'user_id': current_user.id,
            'message': bot_response,
            'is_bot': True,
            'chat_stage': chat_stage,
            'timestamp': datetime.utcnow().isoformat()
        }
        sb_insert('chat_messages', bot_message_data)
        
        return jsonify({
            'response': bot_response,
            'stage': session.get('chat_stage')
        })
        
    except Exception as e:
        logging.error(f"Chat API error: {e}")
        return jsonify({'error': 'An error occurred processing your message'}), 500

def get_ai_response(user_message):
    """Get response from Google Generative AI (google.genai SDK)"""
    try:
        if not genai_client:
            return "I'm sorry, but the AI service is currently unavailable. Please try again later."

        # Get user context
        user_name = session.get('user_name', 'there')
        user_age = session.get('user_age', 'unknown')
        medical_history = session.get('medical_history', 'none provided')

        # Create comprehensive medical prompt
        context = f"""You are Dr. MedBot, an advanced AI medical assistant with expertise in general medicine, preventive care, and health management. You provide evidence-based medical guidance while maintaining professional standards.

PATIENT PROFILE:
- Name: {user_name}
- Age: {user_age} years old
- Medical History: {medical_history}

RESPONSE GUIDELINES:
1. Provide comprehensive, evidence-based medical information
2. Include specific management strategies and lifestyle recommendations
3. Suggest basic over-the-counter medications when appropriate (with dosage guidelines)
4. Offer preventive measures and home remedies
5. Structure responses clearly with sections like:
   - **Assessment**: Brief evaluation of symptoms/condition
   - **Possible Causes**: Common reasons for the condition
   - **Basic Medications**: Safe over-the-counter options with dosages
   - **Home Remedies**: Natural and lifestyle approaches
   - **When to Seek Medical Care**: Red flags requiring professional attention
   - **Prevention**: Strategies to avoid recurrence

MEDICATION GUIDELINES:
- Only suggest common over-the-counter medications (acetaminophen, ibuprofen, antihistamines, etc.)
- Always include proper dosing based on age and weight
- Mention contraindications and side effects
- Emphasize reading labels and following package instructions
- Never suggest prescription medications

IMPORTANT DISCLAIMERS:
- For emergency symptoms (chest pain, difficulty breathing, severe injuries), immediately recommend emergency care
- For persistent, worsening, or concerning symptoms, recommend consulting a healthcare provider
- All medication suggestions are for educational purposes only
- Always read medication labels and consult pharmacists for drug interactions

PATIENT INQUIRY: {user_message}

Please provide a detailed, helpful response that addresses their concern comprehensively while maintaining appropriate medical caution."""

        response = genai_client.models.generate_content(
            model='gemma-3-12b-it',
            contents=context
        )

        return response.text

    except Exception as e:
        logging.error(f"AI response error: {e}")
        return "I'm sorry, I'm having trouble processing your request right now. Please try again or consult with a healthcare professional."

@app.route('/api/chat/history')
@login_required
def chat_history():
    """Get chat history for current user"""
    try:
        messages = sb_select(
            'chat_messages',
            filters={'user_id': f'eq.{current_user.id}'},
            order='timestamp.asc'
        )
        return jsonify([{
            'message': msg['message'],
            'is_bot': msg['is_bot'],
            'timestamp': msg['timestamp'],
            'stage': msg['chat_stage']
        } for msg in messages])
    except Exception as e:
        logging.error(f"Chat history error: {e}")
        return jsonify({'error': 'Failed to load chat history'}), 500

@app.route('/api/chat/reset', methods=['POST'])
@login_required
def reset_chat():
    """Reset chat stage to start over"""
    session['chat_stage'] = 'name'
    session.pop('user_name', None)
    session.pop('user_age', None)
    session.pop('medical_history', None)
    return jsonify({'message': 'Chat reset successfully'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
