# Data storage

The app stores its data in CSV files under `data/`:

- `users.csv` for accounts
- `assets.csv` for assets
- `work_orders.csv` for work orders
- `procurement.csv` for procurement requests
- `notifications.csv` for notifications

No database server or connection credentials are required. The app reads and writes these files directly.
