# Solaren — Cloud Setup

## Βήματα deploy στο Render.com

1. Δημιούργησε λογαριασμό στο github.com
2. Νέο repository → ανέβασε ΟΛΑA τα αρχεία αυτού του φακέλου
3. Πήγαινε στο render.com → New → Web Service
4. Σύνδεσε το GitHub repository
5. Βάλε τα Environment Variables (API keys) από το .env.example
6. Deploy → έτοιμο!

## API Endpoints
- GET /              → Dashboard
- GET /api/all-sites → Όλες οι εγκαταστάσεις + alerts
- GET /api/sungrow/sites
- GET /api/huawei/sites
- GET /api/goodwe/sites
- GET /api/fronius/sites
