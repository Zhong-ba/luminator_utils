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

Arguments:

| Argument | Description |
| --- | --- |
| `ips` | Required path to the source IPS database. |
| `-o`, `--output` | Required path for the generated Axion/DataTransit database. |
| `--seconds SECONDS` | Exposure duration in seconds. Defaults to `2`. |
| `--network-name NAME` | Axion network name, up to 20 characters. Defaults to the IPS filename stem. |
| `--system-name NAME` | Axion system name, up to 20 characters. Defaults to the IPS filename stem. |
| `--index-mode {rebuild,remove,preserve}` | Index handling mode. Defaults to `rebuild`. |
| `--gui` | Opens the desktop interface. With no other arguments, running the script also opens the interface. |

Running the script without arguments opens its desktop interface.

Class-A special messages are selected by code:
Code 2 converts to Axion `ZZZZ` (emergency), and code 21 converts to `$YLD` (yield). Only codes present in the source frame table are included. Other class-A codes are not converted. Axion numeric IDs 9998 and 9997 are converter-assigned output
values, not IPS input codes.