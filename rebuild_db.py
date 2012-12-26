#! /bin/sh --
""":" # Rebuilds (parts of) metadata.db from the metadata.opf files.

exec calibre-debug -e "$0" ${1+"$@"}

Don't run this script while somebody else (e.g. Calibre) is modifying the
library. If you do anyway, you may lose some data. To be safe, exit from
Calibre while this script is running.
!! TODO(pts): Verify this claim, use EXCLUSIVE locking.

TODO(pts): Renumber books with a conflicting ID.
TODO(pts): Add UNIQUE(name) indexes later.
"""

# by pts@fazekas.hu at Wed Dec 26 12:54:02 CET 2012
__author__ = 'pts@fazekas.hu (Peter Szabo)'

import cStringIO
import os
import os.path
import re
import sqlite3
import sys

import calibre
from calibre.customize import builtins
from calibre.library import database2
from calibre.ebooks.metadata import opf2

# Please note that `assert' statements are ignored in this script.
# It's too late to change it back.


def encode_unicode(data):
  if isinstance(data, str):
    return data
  elif isinstance(data, unicode):
    if u'\xfffd' in data:
      raise AssertionError('Uknown character in %r. Please set up system '
                           'locale properly, or use ASCII only.' % data)
    return data.encode(calibre.preferred_encoding)
  else:
    raise TypeError(type(data))


def encode_utf8(data):
  if isinstance(data, str):
    return data
  elif isinstance(data, unicode):
    return data.encode('UTF-8')
  elif data is None:
    return data
  else:
    raise TypeError(type(data))


SQLITE_KEYWORDS = frozenset((
    'ABORT', 'ACTION', 'ADD', 'AFTER', 'ALL', 'ALTER', 'ANALYZE', 'AND',
    'AS', 'ASC', 'ATTACH', 'AUTOINCREMENT', 'BEFORE', 'BEGIN', 'BETWEEN',
    'BY', 'CASCADE', 'CASE', 'CAST', 'CHECK', 'COLLATE', 'COLUMN', 'COMMIT',
    'CONFLICT', 'CONSTRAINT', 'CREATE', 'CROSS', 'CURRENT_DATE',
    'CURRENT_TIME', 'CURRENT_TIMESTAMP', 'DATABASE', 'DEFAULT',
    'DEFERRABLE', 'DEFERRED', 'DELETE', 'DESC', 'DETACH', 'DISTINCT',
    'DROP', 'EACH', 'ELSE', 'END', 'ESCAPE', 'EXCEPT', 'EXCLUSIVE',
    'EXISTS', 'EXPLAIN', 'FAIL', 'FOR', 'FOREIGN', 'FROM', 'FULL', 'GLOB',
    'GROUP', 'HAVING', 'IF', 'IGNORE', 'IMMEDIATE', 'IN', 'INDEX',
    'INDEXED', 'INITIALLY', 'INNER', 'INSERT', 'INSTEAD', 'INTERSECT',
    'INTO', 'IS', 'ISNULL', 'JOIN', 'KEY', 'LEFT', 'LIKE', 'LIMIT', 'MATCH',
    'NATURAL', 'NO', 'NOT', 'NOTNULL', 'NULL', 'OF', 'OFFSET', 'ON', 'OR',
    'ORDER', 'OUTER', 'PLAN', 'PRAGMA', 'PRIMARY', 'QUERY', 'RAISE',
    'REFERENCES', 'REGEXP', 'REINDEX', 'RELEASE', 'RENAME', 'REPLACE',
    'RESTRICT', 'RIGHT', 'ROLLBACK', 'ROW', 'SAVEPOINT', 'SELECT', 'SET',
    'TABLE', 'TEMP', 'TEMPORARY', 'THEN', 'TO', 'TRANSACTION', 'TRIGGER',
    'UNION', 'UNIQUE', 'UPDATE', 'USING', 'VACUUM', 'VALUES', 'VIEW',
    'VIRTUAL', 'WHEN', 'WHERE'))
"""Copied from http://www.sqlite.org/lang_keywords.html"""


SQLITE_BARE_NAME_RE = re.compile(r'[_a-zA-Z]\w*\Z')


def escape_sqlite_name(name):
  if isinstance(name, unicode):
    name = name.decode('UTF-8')
  elif isinstance(name, str):
    name.encode('UTF-8')  # Just to generate UnicodeEncodeError.
  else:
    raise TypeError
  if '\0' in name:
    raise ValueError('NUL in SQLite name: %r' % name)
  if SQLITE_BARE_NAME_RE.match(name) and name.upper() not in SQLITE_KEYWORDS:
    return name
  else:
    return '"%s"' % name.replace('"', '""')


def usage(argv0):
  return ('Rebuilds (parts of) metadata.db from the metadata.opf files.\n'
          'Usage: %s [<calibre-library-dir>]' % argv0)


BOOK_TABLES = (
    'books', 'authors', 'books_authors_link', 'books_languages_link',
    'books_plugin_data', 'books_publishers_link', 'books_ratings_link',
    'books_series_link', 'books_tags_link', 'comments', 'conversion_options',
    'data', 'identifiers', 'languages', 'publishers', 'ratings', 'series',
    'tags', 'metadata_dirtied')
"""The order is irrelevant."""


class DataRow(object):
  table_name = 'data'
  __slots__ = (
      'id',  # INTEGER PRIMARY KEY,
      'book',  # INTEGER NON NULL,
      'format',  # TEXT NON NULL COLLATE NOCASE,
      'uncompressed_size',  # INTEGER NON NULL,
      'name',  # TEXT NON NULL,
  )
  

class BooksRow(object):
  table_name = 'books'
  __slots__ = (
      'id',  # INTEGER PRIMARY KEY AUTOINCREMENT,
      'title',  # TEXT NOT NULL DEFAULT 'Unknown' COLLATE NOCASE,
      'sort',  # TEXT COLLATE NOCASE,
      'timestamp',  # TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      'pubdate',  # TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      'series_index',  # REAL NOT NULL DEFAULT 1.0,
      'author_sort',  # TEXT COLLATE NOCASE,
      'isbn',  # TEXT DEFAULT "" COLLATE NOCASE,
      'lccn',  # TEXT DEFAULT "" COLLATE NOCASE,
      'path',  # TEXT NOT NULL DEFAULT "",
      'flags',  # INTEGER NOT NULL DEFAULT 1
      'uuid',  # TEXT,
      'has_cover',  # BOOL DEFAULT 0,
      'last_modified',  # TIMESTAMP NOT NULL DEFAULT "2000-01-01 00:00:00+00:00"
  )


class AuthorsRow(object):
  table_name = 'authors'
  __slots__ = (
      'id',  # INTEGER PRIMARY KEY,
      'name',  # TEXT NOT NULL COLLATE NOCASE,
      'sort',  # TEXT COLLATE NOCASE,
      'link',  # TEXT NOT NULL DEFAULT "",
  )


class IdentifiersRow(object):
  table_name = 'identifiers'
  __slots__ = (
      'id',  # INTEGER PRIMARY KEY,
      'book',  # INTEGER NON NULL,
      'type',  # TEXT NON NULL DEFAULT "isbn" COLLATE NOCASE,
      'val',  # TEXT NON NULL COLLATE NOCASE,
  )


class LanguagesRow(object):
  table_name = 'languages'
  __slots__ = (
      'id',  # INTEGER PRIMARY KEY,
      'lang_code',  # TEXT NON NULL COLLATE NOCASE,
  )


class CommentsRow(object):
  table_name = 'comments'
  __slots__ = (
      'id',  # INTEGER PRIMARY KEY,
      'book',  # INTEGER NON NULL,
      'text',  # TEXT NON NULL COLLATE NOCASE,
  )


class PublishersRow(object):
  table_name = 'publishers'
  __slots__ = (
      'id',  # INTEGER PRIMARY KEY,
      'name',  # TEXT NOT NULL COLLATE NOCASE,
      'sort',  # TEXT COLLATE NOCASE,
  )


class RatingsRow(object):
  table_name = 'ratings'
  __slots__ = (
      'id',  # INTEGER PRIMARY KEY,
      'rating',  # INTEGER CHECK(rating > -1 AND rating < 11),
  )


class SeriesRow(object):
  table_name = 'series'
  __slots__ = (
      'id',  # INTEGER PRIMARY KEY,
      'name',  # TEXT NOT NULL COLLATE NOCASE,
      'sort',  # TEXT COLLATE NOCASE,
  )


class TagsRow(object):
  table_name = 'tags'
  __slots__ = (
      'id',  # INTEGER PRIMARY KEY,
      'name',  # TEXT NOT NULL COLLATE NOCASE,
  )


class BooksAuthorsLinkRow(object):
  table_name = 'books_authors_link'
  __slots__ = (
      'id',  # INTEGER PRIMARY KEY,
      'book',  # INTEGER NOT NULL,
      'author',  # INTEGER NOT NULL,
  )


class BooksLanguagesLinkRow(object):
  table_name = 'books_languages_link'
  __slots__ = (
      'id',  # INTEGER PRIMARY KEY,
      'book',  # INTEGER NOT NULL,
      'lang_code',  # INTEGER NOT NULL,
      'item_order',  # INTEGER NOT NULL DEFAULT 0,
  )


class BooksPublishersLinkRow(object):
  table_name = 'books_publishers_link'
  __slots__ = (
      'id',  # INTEGER PRIMARY KEY,
      'book',  # INTEGER NOT NULL,
      'publisher',  # INTEGER NOT NULL,
  )


class BooksRatingsLinkRow(object):
  table_name = 'books_ratings_link'
  __slots__ = (
      'id',  # INTEGER PRIMARY KEY,
      'book',  # INTEGER NOT NULL,
      'rating',  # INTEGER NOT NULL,
  )


class BooksSeriesLinkRow(object):
  table_name = 'books_series_link'
  __slots__ = (
      'id',  # INTEGER PRIMARY KEY,
      'book',  # INTEGER NOT NULL,
      'series',  # INTEGER NOT NULL,
  )


class BooksSeriesLinkRow(object):
  table_name = 'books_tags_link'
  __slots__ = (
      'id',  # INTEGER PRIMARY KEY,
      'book',  # INTEGER NOT NULL,
      'tag',  # INTEGER NOT NULL,
  )


BOOK_ROW_CLASSES = (
    DataRow, BooksRow, AuthorsRow, IdentifiersRow, LanguagesRow,
    CommentsRow, PublishersRow, RatingsRow, SeriesRow, TagsRow,
    BooksAuthorsLinkRow, BooksLanguagesLinkRow, BooksPublishersLinkRow,
    BooksRatingsLinkRow, BooksSeriesLinkRow, BooksSeriesLinkRow)
"""The order is irrelevant.

This sequence is used for consistency checks, don't remove elements from it.
"""


def get_extensions(builtins_module):
  mrp = builtins.MetadataReaderPlugin
  extensions = set()
  for name in dir(builtins_module):
    class_obj = getattr(builtins_module, name)
    if type(class_obj) == type(mrp) and issubclass(class_obj, mrp):
      for extension in class_obj.file_types:
        extensions.add(extension.lower().lstrip('.'))
  for extension in extensions:
    if '.' in extensions:
      raise AssertionError(repr(extension))
  extensions.discard('opf')
  # Example: 'rtf', 'prc', 'azw1', 'odt', 'cbr', 'pml', 'rar', 'cbz', 'snb',
  # 'htmlz', 'txt', 'updb', 'zip', 'oebzip', 'chm', 'lit', 'imp', 'html',
  # 'rb', 'fb2', 'docx', 'azw3', 'azw4', 'txtz', 'lrf', 'tpz', 'opf', 'lrx',
  # 'epub', 'mobi', 'pmlz', 'pobi', 'pdf', 'pdb', 'azw'.
  return extensions


EXTENSIONS = frozenset(get_extensions(builtins))


def new_id(ids, row_obj):
  id_int = ids[type(row_obj)] + 1
  ids[type(row_obj)] = id_int
  row_obj.id = id_int


def create_insert_sql(row_class):
  return 'INSERT INTO %s VALUES (%s)' % (
      escape_sqlite_name(row_class.table_name),
      ','.join(['?'] * len(row_class.__slots__)))


def is_correct_book_filename(filename, opf_dir):
  title = os.path.basename(opf_dir)
  author = os.path.basename(os.path.dirname(opf_dir))
  i = filename.rfind('.')
  if i < 0:
    raise AssertionError(repr(filename))
  j = title.rfind(' (')  # 'name (id)'.
  if j < 0:
    raise AssertionError(repr(title))
  filename = filename[:i]
  title = title[:j]
  expected_filename = '%s - %s' % (title, author)
  return filename == expected_filename
  

def main(argv):
  if (len(argv) > 1 and argv[1] in ('--help', '-h')):
    print usage(argv[0])
    sys.exit(0)
  if len(argv) > 2:
    print >>sys.stderr, usage()
    sys.exit(1)
  dbdir = argv[1].rstrip(os.sep) if len(argv) > 1 else '.'
  if os.path.isfile(dbdir):
    dbname = dbdir
    dbdir = os.path.dirname(dbdir)
  else:
    dbname = os.path.join(dbdir, 'metadata.db')
  print >>sys.stderr, 'info: Rebuilding Calibre database: ' + dbname
  if not os.path.isfile(dbname):
    raise AssertionError('Calibre database missing: %s' % dbname)
  dbconn = sqlite3.connect(dbname, check_same_thread=False,
                         isolation_level='EXCLUSIVE')
  dbconn.text_factory = str
  dc = dbconn.cursor()
  dc.execute('PRAGMA synchronous=OFF')
  dc.execute('PRAGMA journal_mode=MEMORY')
  dc.execute('PRAGMA temp_store=MEMORY')
  dc.execute('PRAGMA cache_size=-16384')  # 16 MB.
  dc.execute('BEGIN EXCLUSIVE')  # Locks the file immediately.
  encoding = tuple(dc.execute('PRAGMA encoding'))[0][0].upper()
  if encoding not in ('UTF8', 'UTF-8'):
    # TODO(pts): Maybe UTF16-le etc. also work.
    raise RuntimeError('Unsupported database encoding: %s' % encoding)
  master_by_type = {'table': [], 'index': [], 'view': [], 'trigger': []}
  tables_to_copy = []
  # SELECT type, name, tbl_name, sql FROM sqlite_master;
  for row in dc.execute(
      'SELECT type, sql, name FROM sqlite_master ORDER BY tbl_name, name'):
    if row[0] not in ('index', 'table', 'trigger', 'view'):
      raise AssertionError('Bad type in master: %r' % row[0])
    if not row[2].startswith('sqlite_'):  # e.g. sqlite_sequence.
      # KeyError is deliberate if type (row[0]) is not known.
      master_by_type[row[0]].append(row[1])
    if (row[0] == 'table' and row[2] not in BOOK_TABLES and
        (not row[2].startswith('sqlite_') or row[2] == 'sqlite_sequence')):
      # Don't copy sqlite_stat1 etc.
      tables_to_copy.append(row[2])
  # tables_to_copy for Calibre 0.9.11 is ['custom_columns', 'feeds',
  # 'library_id', 'preferences'].

  newdbname = 'metare.db'
  print >>sys.stderr, 'info: Creating new database: %s' % newdbname
  # TODO(pts): Do full locking on this file.
  open(newdbname, 'w').truncate(0)
  conn = sqlite3.connect(newdbname, check_same_thread=False,
                         isolation_level='EXCLUSIVE')
  conn.text_factory = str
  c = conn.cursor()
  c.execute('PRAGMA synchronous=OFF')
  c.execute('PRAGMA journal_mode=OFF')
  c.execute('PRAGMA count_changes=OFF')
  c.execute('PRAGMA temp_store=MEMORY')
  c.execute('PRAGMA cache_size=-16384')  # 16 MB.
  c.execute('PRAGMA encoding=%s' % escape_sqlite_name(encoding))
  # TODO(pts): Make sure we don't release the lock before or after
  # 'CREATE TABLE' etc. Maybe lock the file descriptor manually. (Can we do
  # that in Python?).
  c.execute('BEGIN EXCLUSIVE')  # Locks the file immediately.
  for sql in master_by_type['table']:
    c.execute(sql)  # 'CREATE TABLE ...'.
  for row_class in BOOK_ROW_CLASSES:
    c.execute('SELECT * FROM %s LIMIT 0' %
              escape_sqlite_name(row_class.table_name))
    fields = tuple(x[0] for x in c.description)
    if fields != row_class.__slots__:
      raise RuntimeError('Unexpected fields in table %s: expected=%r got=%r' %
                         (row_class.table_name, row_class.__slots__, fields))
  c.execute('BEGIN EXCLUSIVE')  # Locks the file immediately.

  # Copy the non-book tables.
  for table in tables_to_copy:
    table_esc = escape_sqlite_name(table)
    dc.execute('SELECT * FROM %s' % table_esc)
    if table == 'sqlite_sequence':
      if (len(dc.description) < 2 or
          dc.description[0][0] != 'name' or
          dc.description[1][0] != 'seq'):
        raise AssertionError
      rows = []
      for row in dc:
        if not isinstance(row[0], str):
          raise AssertionError
        if row[0] not in BOOK_TABLES:
          rows.append(row)
    else:
      rows = list(dc)  # Small amount of data, fits in memory.
    sql = 'INSERT INTO %s VALUES (%s)' % (
        table_esc, ','.join(['?'] * len(dc.description)))
    for row in rows:
      # TODO(pts): Rewrite all c.execute(...) to creating a .dump file
      # manually, and running it with c.executescript(...). Measure if it is
      # actually faster.
      c.execute(sql, row)

  # Generate metadata.opf (in memory) if missing.
  opfs = {}
  dotexts = sorted('.' + extension for extension in EXTENSIONS)
  if '.opf' in dotexts:
    raise AssertionError
  incorrect_file_count = 0
  for row in dc.execute('SELECT path, id FROM books'):
    opf_dir = os.path.join(dbdir, row[0].replace('/', os.sep))
    if opf_dir in opfs:
      raise AssertionError
    if not os.path.exists(os.path.join(opf_dir, 'metadata.opf')):
      opfs[opf_dir] = row[1]
  if opfs:
    print >>sys.stderr, (
        'info: Computing in-memory metadata.opf for %d book%s.' %
        (len(opfs), 's' * (len(opfs) != 1)))
    db = database2.LibraryDatabase2(dbdir)  # Slow, reads metadata.db.
    for i, opf_dir in sorted((b, a) for a, b in opfs.iteritems()):
      # TODO(pts): Why does this work if this process already holds an
      # EXCLUSIVE lock on metadata.db?
      mi = db.get_metadata(i, index_is_id=True)
      if mi.has_cover and not mi.cover:
        mi.cover = 'cover.jpg'
      opf_data = opf2.metadata_to_opf(mi)
      # TODO(pts): Properly not ignore books with wrong name:
      #   ebook-hu-mate/Thomas\ Mann/Kiralyi\ fenseg\ \(42\)/
      #   Francois Villon balladai - Francois Villon.mobi
      #   Kiralyi fenseg - Thomas Mann.epub
      #   Kiralyi fenseg - Thomas Mann.mobi
      filenames = []
      for filename in os.listdir(opf_dir):
        if (os.path.isfile(os.path.join(opf_dir, filename)) and
            filename != 'cover.jpg' and
            any(1 for dotext in dotexts if filename.endswith(dotext))):
          if is_correct_book_filename(filename, opf_dir):
            filenames.append(
                (filename, os.stat(os.path.join(opf_dir, filename)).st_size))
          else:
            incorrect_file_count += 1
      filenames.sort()
      opfs[opf_dir] = (opf_data, filenames)
      del opf_data, filenames
    del db
  else:
    print >>sys.stderr, 'info: Found metadata.opf for all books in metadata.db.'

  # Read all .opf files.
  print >>sys.stderr, 'info: Reading metadata.opf files.'
  for dirpath, dirnames, filenames0 in os.walk(dbdir):
    if 'metadata.opf' in filenames0 and dirpath != dbdir:
      if dirpath in opfs:
        raise AssertionError('Book directory already defined: %r' % dirpath)
      opf_data = open(os.path.join(dirpath, 'metadata.opf')).read()
      filenames = []
      for filename in filenames0:
        if (filename != 'cover.jpg' and
            any(1 for dotext in dotexts if filename.endswith(dotext))):
          if is_correct_book_filename(filename, dirpath):
            filenames.append(
                (filename, os.stat(os.path.join(dirpath, filename)).st_size))
          else:
            incorrect_file_count += 1
      filenames.sort()
      opfs[dirpath] = (opf_data, filenames)
      del opf_data, filenames
  file_count = sum(len(x[1]) for x in opfs.itervalues())
  print >>sys.stderr, 'info: Found %d e-book file%s and discarded %d.' % (
      file_count, 's' * (file_count != 1), incorrect_file_count)

  # Parse .opf files.
  # TODO(pts): This is the slowest part. Reimplement the metadata parsing, or
  # use the even slower (2x) but simpler solution: db.set_metadata.
  # TODO(pts): What if just making a few books dirty, and let calibre reread.
  print >>sys.stderr, 'info: Parsing %d metadata.opf file%s.' % (
      len(opfs), 's' * (len(opfs) != 1))
  for opf_dir in sorted(opfs):
    opf_data, filenames = opfs[opf_dir]
    opfs[opf_dir] = (opf2.OPF(cStringIO.StringIO(opf_data)).to_book_metadata(),
                     filenames)

  # Add books to the new database.
  # TODO(pts): Split this function.
  print >>sys.stderr, 'info: Adding books to the new database.'
  ids = {
      BooksRow: 0,
      AuthorsRow: 0,
      BooksAuthorsLinkRow: 0,
      DataRow: 0,
  }
  authors_by_name = {}
  books_sql = create_insert_sql(BooksRow)
  authors_sql = create_insert_sql(AuthorsRow)
  data_rows = []
  for opf_dir in sorted(opfs):
    # `mi' contains strings as unicode.
    mi, filenames = opfs[opf_dir]
    #print mi._data
    books_row = BooksRow()
    # TODO(pts): Try to preserve the calibre-id (not available in mi).
    new_id(ids, books_row)
    if mi.title_sort is None:
      raise AssertionError
    books_row.title = encode_utf8(mi.title) or '?'
    books_row.sort = encode_utf8(mi.title_sort)
    books_row.timestamp = mi.timestamp
    books_row.pubdate = mi.pubdate
    if mi.series_index is None:
      books_row.series_index = 1.0
    else:
      # TODO(pts): Test this.
      books_row.series_index = float(mi.series_index)
    if mi.author_sort is None:
      raise AssertionError
    # TODO(pts): Test with a book with multiple authors.
    for author in mi.authors:
      author = encode_utf8(author)
      author_id = authors_by_name.get(author)
      if author_id is None:
        authors_row = AuthorsRow()
        new_id(ids, authors_row)
        authors_by_name[author] = author_id = authors_row.id
        authors_row.name = author
        authors_row.sort = encode_utf8(  # TODO(pts): Is this correct?
            mi.author_sort_map.get(author, mi.author_sort))
        authors_row.link = encode_utf8(  # TODO(pts): Is this correct?
            mi.author_link_map.get(author, ''))  # Seems to be always ''.
        c.execute(authors_sql, [
            getattr(authors_row, name) for name in authors_row.__slots__])
      xid = ids[BooksAuthorsLinkRow] + 1
      ids[BooksAuthorsLinkRow] = xid
      c.execute('INSERT INTO books_authors_link VALUES (?,?,?)',
                (xid, books_row.id, author_id))
    # TODO(pts): Use mi.author_sort_map, mi.author_link_map (if there are
    # multiple authors).
    books_row.author_sort = encode_utf8(mi.author_sort)
    books_row.isbn = encode_utf8(mi.isbn)
    books_row.lccn = None  # Calibre doesn't seem to use this field.
    books_row.path = opf_dir.replace('/', os.sep)
    books_row.flags = 1  # Calibre doesn't seem to use this field.
    books_row.uuid = encode_utf8(mi.uuid)
    books_row.has_cover = False
    for guide in mi.guide or ():
      if getattr(guide, 'type', None) == 'cover':
        books_row.has_cover = True
        break
    books_row.last_modified = mi.last_modified  # Usually None.
    if books_row.last_modified is None:
      # This information is missing from the .opf file.
      # TODO(pts): Get it from the mtime of the .opf file? git doesn't preserve
      # it in the working copy, but maybe we could get it.
      books_row.last_modified = '2000-01-01 00:00:00+00:00'
    c.execute(books_sql, [
        getattr(books_row, name) for name in books_row.__slots__])
    for filename, filesize in filenames:
      i = filename.rfind('.')
      data_row = DataRow()
      new_id(ids, data_row)
      data_row.book = books_row.id
      data_row.format = filename[i + 1:].upper()  # TODO(pts): Apply a map, e.g. AZW1 to TOPAZ etc.?
      data_row.name = filename[:i]
      # These are not the same numbers, but they are a good approximation.
      data_row.uncompressed_size = filesize
      data_rows.append(data_row)
  data_sql = create_insert_sql(DataRow)
  for data_row in data_rows:
    c.execute(data_sql, [
        getattr(data_row, name) for name in data_row.__slots__])
  del data_rows

  # TODO(pts): Populate table identifiers.
  # TODO(pts): Populate table languages.
  # TODO(pts): Populate table comments.
  # TODO(pts): Populate table publishers.
  # TODO(pts): Populate table ratings.
  # TODO(pts): Populate table series.
  # TODO(pts): Populate table tags.
  # TODO(pts): Populate table books_languages_link.
  # TODO(pts): Populate table books_publishers_link.
  # TODO(pts): Populate table books_ratings_link.
  # TODO(pts): Populate table books_series_link.
  # TODO(pts): Populate table books_tags_link.

  # Add functions and aggregates needed by the indexes, views and triggers
  # below.
  #
  # TODO(pts): Automate these by catching `OperationalError: no such
  #   function: title_sort' etc.
  conn.create_aggregate('sortconcat', 2, None)
  conn.create_function('concat', 1, None)
  conn.create_function('books_list_filter', 1, None)
  conn.create_function('title_sort', 1, None)

  # Create indexes, views and triggers.
  #
  # It's important to create the indexes after the INSERT INTO operations, so
  # the INSERT INTO operations become fast.
  conn.commit()
  for sql in master_by_type['index']:
    c.execute(sql)  # 'CREATE INDEX ...'.
  for sql in master_by_type['view']:
    c.execute(sql)  # 'CREATE VIEW ...'.
  for sql in master_by_type['trigger']:
    c.execute(sql)  # 'CREATE TRIGGER ...'.

  print >>sys.stderr, 'info: Generating statistics.'
  c.execute('ANALYZE')
  conn.commit()
  conn.close()
  print >>sys.stderr, 'info: Done.'


if __name__ == '__main__':
  # SUXX: Original, byte argv not available.
  main(map(encode_unicode, sys.argv))
