.. _commands-barman-get-wal:

``barman get-wal``
""""""""""""""""""

Synopsis
^^^^^^^^

.. code-block:: text
    
    get-wal
        [ -j ]
        [ -o  OUTPUT_DIRECTORY ]
        [ -p  VALUE ]
        [ { -P | --partial } ]
        [ { -t | --test } ]
        [ -z ]
        SERVER_NAME WAL_NAME

Description
^^^^^^^^^^^

Retrieve a WAL file from the xlog archive of a specified server. By default, if the
requested WAL file is found, it is returned as uncompressed content to ``STDOUT``.

Parameters
^^^^^^^^^^

``SERVER_NAME``
    Name of the server in barman node

``WAL_NAME``
    Id of the backup in barman catalog.

``-j`` /  ``--bzip2``
    Output will be compressed using bzip2.

``-z`` / ``--gzip``
    Output will be compressed using gzip.

``--keep-compression``
    Do not uncompress the file content. The output will be the original compressed
    file.

``-o``
    Destination directory where barman will store the WAL file.

``-p`` 
    Specify an integer value greater than or equal to 1 to retrieve WAL files from the
    specified WAL file up to the value specified by this parameter. When using this option,
    ``get-wal`` returns a list of zero to the specified WAL segment names, with one name
    per row.

``-P`` / ``--partial``
    Additionally, collect partial WAL files (.partial).

``-t`` / ``--test``
    Test both the connection and configuration of the specified Postgres server in
    Barman for WAL retrieval. When this option is used, the required ``WAL_NAME``
    argument is disregarded.


.. warning::

    ``-z`` / ``--gzip`` and ``-j`` /  ``--bzip2`` options are deprecated and will be
    removed in the future. For WAL compression, please make sure to enable it directly
    on the Barman server via the ``compression`` configuration option.
