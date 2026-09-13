# Luminator Utilities

Small tools for working with Luminator IPS databases.

- `luminator_ips_export.py` exports sign messages as PNG files.
- `luminator_to_axion.py` converts an IPS database to an Axion/DataTransit database.

## Setup

Requires Python 3 and Pillow.

```powershell
py -m pip install pillow
```

## Export PNGs

Open the desktop interface:

```powershell
py luminator_ips_export.py
```

Or export from the command line:

```powershell
py luminator_ips_export.py path\to\signs.ips -o output_folder
```

PNG files are exported at the sign's native dot resolution by default. Add
`--scale 2` to double their size, or `--first-exposure-only` to export just
the first exposure for each sign.

## Convert to Axion

```powershell
py luminator_to_axion.py path\to\signs.ips -o output_database
```

Running the script without arguments opens its desktop interface.