# brkraw-backup

CLI plugin that adds `brkraw backup`: archive raw Bruker ParaVision study folders
as zip files, check them, and repair or retire them safely.

**brkraw-backup never deletes a raw folder.** When an archive has passed
`verify --level content`, you delete the raw folder yourself.

## Getting started

Install the plugin:

```bash
pip install -U git+ssh://git@github.com/BrkRaw/brkraw-backup.git
```

Save the raw and archive folders in `~/.brkraw/config.yaml`:

```bash
brkraw backup init /path/to/raw_dir /path/to/archive_dir
```

Then:

```bash
brkraw backup status --scan        # look at both folders and record what is there
brkraw backup create               # archive every raw folder that has no archive yet
brkraw backup verify --all         # read every archive and check its CRCs
```

Every command can take `--rawdata DIR` and `--archive DIR` instead of the saved
paths, and `--dry-run` to show what would happen without changing anything.

## Commands

| Command | What it does | Changes files? |
| --- | --- | --- |
| `status` | Shows the recorded state. `--scan` looks again, `--issues` shows only problems. | registry only (`--scan`) |
| `create [KEY...]` | Makes `<KEY>.zip` for raw folders without an archive, then verifies it at `content` level. | new archives only |
| `verify KEY... \| --all` | Checks archives at `--level list`, `crc` (default) or `content`. | registry only |
| `repair KEY...` | Rebuilds archives from raw; the old archive is kept in the trash. | the named archives |
| `remove KEY...` | Moves archives into the trash. | the named archives |
| `purge KEY...` | Permanently deletes trash copies of the named keys, after confirmation. | the trash only |
| `init RAW ARCHIVE` | Saves the two folders in brkraw's config. | config only |
| `migrate`, `about` | Advanced: import a 0.3.x cache; show versions and paths. | registry (`migrate`) |

A *KEY* is the raw folder name; its archive is `<KEY>.zip` (or a ParaVision
`<KEY>.PvDatasets` export) in the archive folder.

`repair`, `remove` and `purge` need at least one KEY; there is no "all archives"
default. When several KEYs are given, every one is checked before anything
changes: an unknown name, a name with two archives, or a name with unfinished
work stops the whole call (exit code 2).

### `status`

```bash
brkraw backup status                  # from the registry (no folder scan)
brkraw backup status --scan           # look at raw and archive folders again
brkraw backup status --scan --issues  # only datasets with a problem, with the problem
brkraw backup status --status TODO,UNFINISHED
```

Columns: scan counts, sizes, `BACKUP_AT`, `STATUS`, and the last verification
(`VERIFY` such as `crc:OK` or `content:FAIL`, and `VERIFY_AT`). Problems include
`duplicate_archive` (a KEY has both `.zip` and `.PvDatasets`),
`unfinished:<op>:<step>` (an interrupted `repair`/`remove`) and `partial_file`
(an interrupted `create`). Unfinished work is shown even without `--scan`.

`status --scan` loads each folder with brkraw to count scans; it is a quick look,
not a verification. Use `verify` for that.

### `create`

```bash
brkraw backup create              # every raw folder without an archive
brkraw backup create S1 S2        # only these
brkraw backup create --dry-run
```

`create` never touches an existing archive: a KEY that already has one is
skipped. It writes `<KEY>.zip.partial`, checks it at `content` level against the
raw folder, and only then puts it in place as `<KEY>.zip`. If the check fails (for
example the raw folder changed while it was being archived), nothing is left
behind and the exit code is 1.

### `verify`

```bash
brkraw backup verify S1                    # crc level
brkraw backup verify --all
brkraw backup verify S1 --level content    # also compare with the raw folder
brkraw backup verify S1 --path S1.PvDatasets
```

| Level | Checks | Needs raw | Reads |
| --- | --- | --- | --- |
| `list` | file names and sizes match the raw folder | yes | archive directory only |
| `crc` (default) | every member reads back and matches its stored CRC-32 | no | the whole archive |
| `content` | `crc`, plus a CRC-32 of every raw file compared with the archive's | yes | the whole archive and the whole raw folder |

Failure reasons: `unreadable_zip` (the zip directory cannot be read),
`crc_mismatch`, `bad_data` (compressed data cannot be decoded),
`duplicate_member`, `missing_files`, `extra_files`, `size_mismatch`,
`content_mismatch` (same size, different content), `raw_changed` (the raw folder
changed during the check: a file's size, modification time, change time or inode
differs between the start and the end, or the folder could not be read). `list` and `content` refuse a KEY without a raw folder
(exit code 2); with `--all` such keys are skipped with a warning.

`content` compares CRC-32 values, so two different files with the same CRC-32
(about 1 in 4.3 billion) would not be told apart. It protects against accidents,
not against deliberate tampering.

### `repair`

```bash
brkraw backup repair S1
brkraw backup repair S1 --dry-run
brkraw backup repair S1 --path S1.zip   # when S1 has two archives
```

For each KEY, in this order, with a journal in
`<archive>/.brkraw-backup-journal/<KEY>.json`:

1. write `<KEY>.zip.partial` from the raw folder;
2. check it at `content` level (if it fails, the partial file is removed and the
   old archive stays as it was);
3. put a copy of the old archive in `<archive>/.brkraw-backup-trash/<KEY>/<UTC time>/`
   (a hard link on the same disk, otherwise a copy that is compared byte by byte);
4. replace the archive with the new file in one step (`os.replace`).

The archive path always holds a whole archive: the old one before step 4, the
checked new one after it. If a run stops part way, `status` shows
`unfinished:repair:<step>` and running `repair KEY` again finishes it. If it
stopped after step 4 and the raw folder has changed since, the rerun finishes on a
`crc` check of the new archive and tells you to run `repair` again to include the
change. `repair`
refuses a KEY whose raw folder is missing (exit code 2); nothing is changed.

### `remove` and `purge`

```bash
brkraw backup remove S1          # move S1's archive into the trash
brkraw backup purge S1           # asks: type "yes" to delete permanently
brkraw backup purge S1 --yes
```

`remove` only moves the archive into `<archive>/.brkraw-backup-trash/<KEY>/<UTC time>/`
in one step; nothing is deleted. `purge` deletes only what is in the trash for the
named KEYs. It lists the paths and sizes first and needs `yes` (or `--yes`). If a
deletion fails it stops and tells you what was already deleted.

### Exit codes

`0` done, `1` a check or step failed (the message says what; unfinished work keeps
its journal), `2` nothing was done (bad name, missing raw for `repair`, unknown
command, refused confirmation).

## Changes in 0.2.0

The command set was replaced without aliases:

| Removed | Use instead |
| --- | --- |
| `info`, `registry` | `status` |
| `scan` | `status --scan` |
| `review` | `status --scan --issues` |
| `run` | `create` (to rebuild an archive: `repair KEY`) |
| `run --delete-raw` | removed; delete raw folders yourself after `verify --level content` |
| `run --rebuild` | `repair KEY` |
| `--integrity` | `verify --level list` / `content` |

The old names no longer exist anywhere in the code: typing one gives argparse's
ordinary "invalid choice" error, exit code 2, and nothing is changed. `status`, `create`, `verify`, `repair`, `remove` and
`purge` take the folders from the config or from `--rawdata`/`--archive` (not as
positional arguments). The registry keeps entries written by 0.1.x.

## Files in the archive folder

| Path | Written by |
| --- | --- |
| `<KEY>.zip` | `create`, `repair` |
| `<KEY>.zip.partial` | `create`, `repair` while writing |
| `.brkraw-backup-registry.json` | `status --scan`, `create`, `verify`, `repair`, `remove`, `purge`, `migrate` (not with `--dry-run`) |
| `.brkraw-backup-journal/<KEY>.json` | `repair`, `remove` while running |
| `.brkraw-backup-trash/<KEY>/<UTC time>/` | `repair`, `remove`; emptied by `purge` |

Everything brkraw-backup writes stays in the archive folder, so the archive
folder may not be the raw folder or inside it, and `--registry` must be a plain
file name (both refused with exit code 2). New files are created fresh: a stale
`.partial` or temporary file, or a link someone left under that name, is removed
by name first and never written through. A journal whose paths are not exactly
this key's own files is refused, and nothing is changed.

Running two brkraw-backup commands on the same archive folder at the same time is
not supported.
