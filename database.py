import sqlite3
import json
import io
from PIL import Image


class LibraryDB:
    def __init__(self, db_path="xlibre.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.create_tables()
        self.check_and_migrate()  # Auto-fix schema on startup

    def create_tables(self):
        # 1. Books Table
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS books (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            author TEXT,
            description TEXT,
            genre TEXT,
            publisher TEXT,
            publish_date TEXT,
            path_epub TEXT UNIQUE,
            path_xtc TEXT,
            cover_blob BLOB,
            render_settings TEXT, 
            status TEXT DEFAULT 'Unread',
            added_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        # 2. NEW: App Settings Table (Key-Value Store)
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """)
        self.conn.commit()

    def close(self):
        """Closes the SQLite database connection safely."""
        if self.conn:
            self.conn.close()

    def check_and_migrate(self):
        """Checks for missing columns and adds them automatically."""
        cursor = self.conn.cursor()

        # Ensure app_settings table exists (for users upgrading)
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """)

        # Existing migration logic for books...
        cursor.execute("PRAGMA table_info(books)")
        columns = [info[1] for info in cursor.fetchall()]

        required = {
            "description": "TEXT",
            "genre": "TEXT",
            "publisher": "TEXT",
            "publish_date": "TEXT",
            "cover_blob": "BLOB",
            "render_settings": "TEXT",
            "status": "TEXT DEFAULT 'Unread'"
        }

        for col, col_type in required.items():
            if col not in columns:
                try:
                    cursor.execute(f"ALTER TABLE books ADD COLUMN {col} {col_type}")
                    print(f"Migrated: Added column '{col}'")
                except Exception as e:
                    print(f"Migration error for {col}: {e}")

        self.conn.commit()

    # --- NEW: Settings Methods ---

    def set_config(self, key, value):
        """Saves a setting (e.g., 'view_mode' -> 'list')"""
        self.conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (key, str(value)))
        self.conn.commit()

    def get_config(self, key, default=None):
        """Retrieves a setting."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
        res = cursor.fetchone()
        return res[0] if res else default

    # --- Existing Book Methods (Unchanged) ---

    def update_book_status(self, book_id, status):
        self.conn.execute("UPDATE books SET status = ? WHERE id = ?", (status, book_id))
        self.conn.commit()

    def add_book(self, epub_path, title, author, description, genre, publisher, publish_date, cover_image_obj):
        blob = None
        if cover_image_obj:
            thumb = cover_image_obj.copy()
            thumb.thumbnail((200, 300))
            img_byte_arr = io.BytesIO()
            thumb.save(img_byte_arr, format='PNG')
            blob = img_byte_arr.getvalue()

        cursor = self.conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO books (path_epub, title, author, description, genre, publisher, publish_date, cover_blob, render_settings)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (epub_path, title, author, description, genre, publisher, publish_date, blob, "{}"))
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def get_all_books(self):
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT id, title, author, path_epub, path_xtc, cover_blob, render_settings, 
            description, genre, publisher, publish_date, added_date, status 
            FROM books ORDER BY added_date DESC
        """)
        return cursor.fetchall()

    def delete_book(self, book_id):
        self.conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
        self.conn.commit()

    def update_settings(self, book_id, settings_dict):
        json_str = json.dumps(settings_dict)
        self.conn.execute("UPDATE books SET render_settings = ? WHERE id = ?", (json_str, book_id))
        self.conn.commit()

    def update_xtc_path(self, book_id, xtc_path):
        self.conn.execute("UPDATE books SET path_xtc = ? WHERE id = ?", (xtc_path, book_id))
        self.conn.commit()

    def update_book_details(self, book_id, desc, genre, pub, date, cover_blob, title=None, author=None):
        params = [desc, genre, pub, date]
        query = "UPDATE books SET description=?, genre=?, publisher=?, publish_date=?"
        if title is not None:
            query += ", title=?"
            params.append(title)
        if author is not None:
            query += ", author=?"
            params.append(author)
        query += " WHERE id=?"
        params.append(book_id)
        self.conn.execute(query, tuple(params))
        if cover_blob:
            self.conn.execute("UPDATE books SET cover_blob=? WHERE id=?", (cover_blob, book_id))
        self.conn.commit()