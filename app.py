# app.py

from flask import Flask, render_template, request, redirect, url_for, session
from werkzeug.utils import secure_filename
import os
from flask import jsonify
from populate_database import process_pdfs_and_populate_database, clear_database
from get_embedding_function import get_embedding_function
from langchain_chroma import Chroma
from langchain.prompts import ChatPromptTemplate
from langchain_ollama import OllamaLLM
from flask_wtf.csrf import CSRFProtect, generate_csrf

app = Flask(__name__)
app.secret_key = 'your_secret_key'  # Replace with a secure random value
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB max file size

csrf = CSRFProtect(app)

@app.context_processor
def inject_csrf_token():
    return dict(csrf_token=generate_csrf())


CHROMA_PATH = "chroma"

PROMPT_TEMPLATE = """
Answer the question based only on the following context:

{context}

---

Answer the question based on the above context: {question}
"""

def query_rag(query_text: str):
    # Prepare the DB.
    embedding_function = get_embedding_function()
    db = Chroma(persist_directory=CHROMA_PATH, embedding_function=embedding_function)

    # Search the DB.
    results = db.similarity_search_with_score(query_text, k=5)

    context_text = "\n\n---\n\n".join([doc.page_content for doc, _ in results])
    prompt_template = ChatPromptTemplate.from_template(PROMPT_TEMPLATE)
    prompt = prompt_template.format(context=context_text, question=query_text)

    model = OllamaLLM(model="llama3.2")
    # model = OllamaLLM(
    # model="llama3.2",
    # temperature=0.7,
    # max_tokens=512,
    # top_p=0.9,
    # repetition_penalty=1.1
    # )

    response_text = model.invoke(prompt)

    sources = [doc.metadata.get("id", None) for doc, _ in results]

    return response_text, sources

def get_uploaded_files():
    if os.path.exists(app.config['UPLOAD_FOLDER']):
        return os.listdir(app.config['UPLOAD_FOLDER'])
    else:
        return []

@app.route('/', methods=['GET', 'POST'])
def home():
    embeddings_created = session.get('embeddings_created', False)
    processing = session.get('processing', False)
    response = None
    sources = None
    question = None

    if embeddings_created and request.method == 'POST':
        # Handle question submission
        question = request.form['question']
        response, sources = query_rag(question)
        uploaded_files = get_uploaded_files()
        return render_template('home.html', embeddings_created=True, response=response, sources=sources, question=question, uploaded_files=uploaded_files)
    elif embeddings_created:
        uploaded_files = get_uploaded_files()
        return render_template('home.html', embeddings_created=True, uploaded_files=uploaded_files)
    else:
        return render_template('home.html', embeddings_created=False, processing=processing)

@app.route('/upload', methods=['GET', 'POST'])
def upload():
    if request.method == 'POST':
        if 'pdf_files' not in request.files:
            return redirect(url_for('home'))

        files = request.files.getlist('pdf_files')
        filepaths = []

        for file in files:
            if file.filename == '':
                continue
            filename = secure_filename(file.filename)
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            filepaths.append(filepath)

        if filepaths:
            # Set processing flag
            session['processing'] = True
            session.modified = True  # Ensure session changes are saved

            # Process uploaded PDFs using functions from populate_database.py
            process_pdfs_and_populate_database(filepaths)

            # Clear processing flag and set embeddings_created flag
            session['processing'] = False
            session['embeddings_created'] = True
            session.modified = True  # Ensure session changes are saved

            # Files are retained in uploads directory for listing

        return redirect(url_for('home'))
    else:
        # Render upload page
        return render_template('upload.html')

@app.route('/reset_rag', methods=['POST'])
def reset_rag():
    # Clear the database and uploaded files
    clear_database()
    if os.path.exists(app.config['UPLOAD_FOLDER']):
        for filename in os.listdir(app.config['UPLOAD_FOLDER']):
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            if os.path.isfile(file_path):
                os.remove(file_path)
    session.clear()
    return redirect(url_for('home'))

@app.route('/ask_question', methods=['POST'])
def ask_question():
    question = request.form['question']
    response, sources = query_rag(question)
    # Return a JSON response
    return jsonify({'response': response, 'sources': sources})


@app.route('/upload_page', methods=['GET'])
def upload_page():
    return render_template('upload.html')

if __name__ == '__main__':
    # Ensure upload and chroma folders exist
    if not os.path.exists(app.config['UPLOAD_FOLDER']):
        os.makedirs(app.config['UPLOAD_FOLDER'])
    if not os.path.exists(CHROMA_PATH):
        os.makedirs(CHROMA_PATH)

    app.run(debug=True)
