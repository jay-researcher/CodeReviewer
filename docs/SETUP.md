# CodeReviewer Setup Guide

## 1. Install Dependencies

```bash
pip install -r requirements.txt
```

## 2. Configure GitLab Access

### Get Your Personal Access Token

1. Go to: https://gitlab.tx-tech.com/-/user_settings/personal_access_tokens
2. Create a new token with scopes:
   - `api` (full API access)
   - `read_api` (read-only API)
   - `read_repository` (read repo content)
3. Copy the token (you'll see it only once)

### Create .env File

Copy the template and add your token:

```bash
cp .env.example .env
```

Edit `.env` and replace `glpat-xxxxxxxxxxxx` with your actual token:

```
GITLAB_TOKEN=glpat-xxxYourActualTokenxxx
GITLAB_URL=https://gitlab.tx-tech.com
```

**⚠️ Important:** Add `.env` to `.gitignore` to avoid committing secrets!

## 3. Run Code Review

### Review from GitLab MR URL

```bash
python review.py \
  --mr-url https://gitlab.tx-tech.com/wvp-sv/dps11/microsrvs/wvadmin/-/merge_requests/198 \
  --jira ECHNL-5552
```

### Review from Local Repository

```bash
python review.py \
  --repo /path/to/local/repo \
  --source-branch feature/DAO#ECHNL-5552 \
  --target-branch 1.4.68.4 \
  --jira ECHNL-5552
```

### Post Report to GitLab (Optional)

Add `--post-gitlab-comment --yes` only after reading the generated report:

```bash
python review.py \
  --mr-url https://gitlab.tx-tech.com/wvp-sv/dps11/microsrvs/wvadmin/-/merge_requests/198 \
  --jira ECHNL-5552 \
  --post-gitlab-comment \
  --yes
```

## 4. Output

Reports are saved to `reports/` directory as Markdown files.

## Troubleshooting

### GitLab API Error 401: Unauthorized
- Token is missing or incorrect
- Check `.env` file has correct `GITLAB_TOKEN`
- Verify token has `api` scope

### GitLab API Error 404: Project Not Found
- Token is not set (returns 404 without auth)
- Project path is incorrect
- User doesn't have access to the project

### Connection Timeout
- GitLab server is down or unreachable
- Check `GITLAB_URL` in `.env`
- Verify network connectivity
