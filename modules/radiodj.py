"""
RadioDJ database integration for VoiceTrack Magic.
Handles connection, metadata lookup, and song export to RadioDJ songs table.
"""

import datetime


def get_connection(host, port, user, password, database):
    """Return a pymysql connection or raise on failure."""
    try:
        import pymysql
        conn = pymysql.connect(
            host=host,
            port=int(port),
            user=user,
            password=password,
            database=database,
            charset='utf8mb4',
            connect_timeout=3,
        )
        return conn
    except ImportError:
        raise RuntimeError(
            "pymysql is not installed.\n"
            "Run: pip install pymysql"
        )


def fetch_song_types(conn):
    """Return list of (id, name) from category table."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, name FROM category ORDER BY name")
        return cur.fetchall()


def fetch_subcategories(conn):
    """Return list of (id, parentid, name) from subcategory table."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, parentid, name FROM subcategory ORDER BY name")
        return cur.fetchall()


def fetch_genres(conn):
    """Return list of (id, name) from genre table."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, name FROM genre ORDER BY name")
        return cur.fetchall()


def get_audio_duration(file_path):
    """Return duration in seconds (float) using pydub."""
    from pydub import AudioSegment
    seg = AudioSegment.from_file(file_path)
    return len(seg) / 1000.0


def _column_exists(conn, table, column):
    """Check whether a column exists in a table."""
    try:
        with conn.cursor() as cur:
            cur.execute(f"SHOW COLUMNS FROM `{table}` LIKE %s", (column,))
            return cur.fetchone() is not None
    except Exception:
        return False


def export_to_radiodj(conn, file_path, settings):
    """
    Insert a row into the RadioDJ songs table.
    Compatible with RadioDJ 2.x and 3.x (handles the new bs_loudness column).
    """
    import os
    # Ensure Windows-style backslash path
    file_path = os.path.normpath(os.path.abspath(file_path))

    if not os.path.exists(file_path):
        raise FileNotFoundError(
            f"File not found before RadioDJ export:\n{file_path}\n"
            "The file must exist at the exact path stored in RadioDJ."
        )

    # RadioDJ 3.x adds a bs_loudness column to the songs table
    has_bs_loudness = _column_exists(conn, 'songs', 'bs_loudness')
    now        = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    year       = datetime.datetime.now().year
    null_date  = "2002-01-01 00:00:01"

    duration   = get_audio_duration(file_path)
    dur_str    = f"{duration:.5f}"
    cue_times  = f"&sta=0.0&end={dur_str}"

    artist     = settings.get('radio_name', '')
    title      = settings.get('filename_title', '')
    song_type  = settings.get('song_type_id', 0)
    id_subcat  = settings.get('subcategory_id', 0)
    id_genre   = settings.get('genre_id', 0)
    label      = settings.get('label', '')
    publisher  = settings.get('publisher', '')
    copyright_ = settings.get('copyright', '')
    comments   = settings.get('comments', '')
    overlay    = 1 if settings.get('overlay', False) else 0

    # bs_loudness is placed right after loudness in RadioDJ 3.x
    bs_loudness_col = ", bs_loudness" if has_bs_loudness else ""
    bs_loudness_val = ", 0.000"       if has_bs_loudness else ""

    sql = f"""
        INSERT INTO songs (
            path, enabled, date_added, date_modified, date_played,
            artist_played, album_played, title_played,
            count_played, play_limit, limit_action,
            start_date, end_date, startEvent, endEvent,
            song_type, id_subcat, id_genre,
            weight, duration, original_duration, cue_times,
            precise_cue, fade_type, start_type, end_type, mix_type,
            mood, gender, lang, rating, loudness{bs_loudness_col}, bs1770, overlay,
            artist, associated_artists, original_artist,
            title, alternate_title, album, composer,
            label, year, track_no, disc_no,
            publisher, copyright, isrc, bpm, comments,
            sweepers, image, buy_link, url1, url2,
            tdate_played, tartist_played, ttitle_played, originalmetadata
        ) VALUES (
            %s, 1, %s, %s, %s,
            %s, %s, %s,
            0, 0, 0,
            %s, %s, 0, 0,
            %s, %s, %s,
            0.0, %s, %s, %s,
            1, 0, 0, 0, 0,
            'Not Set', 'Not Set', 'Not Set', 0, 1.0{bs_loudness_val}, 0.0, %s,
            %s, '', '',
            %s, '', '', '',
            %s, %s, 1, 1,
            %s, %s, '', 0.0, %s,
            '', 'no_cover_image.jpg', '', '', '',
            %s, %s, %s, 0
        )
    """

    params = (
        file_path,          # path
        now,                # date_added
        null_date,          # date_modified
        null_date,          # date_played
        null_date,          # artist_played
        null_date,          # album_played
        null_date,          # title_played
        null_date,          # start_date
        null_date,          # end_date
        song_type,          # song_type
        id_subcat,          # id_subcat
        id_genre,           # id_genre
        dur_str,            # duration
        dur_str,            # original_duration
        cue_times,          # cue_times
        overlay,            # overlay
        artist,             # artist
        title,              # title
        label,              # label
        year,               # year
        publisher,          # publisher
        copyright_,         # copyright
        comments,           # comments
        null_date,          # tdate_played
        null_date,          # tartist_played
        null_date,          # ttitle_played
    )

    with conn.cursor() as cur:
        cur.execute(sql, params)
    conn.commit()
