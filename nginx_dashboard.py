import os
import subprocess
import json
import streamlit as st
import pandas as pd
from datetime import datetime
import plotly.express as px
import shutil
import re

def parse_nginx_log(log_path):
    ip_regex = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$|^[a-fA-F0-9:]+$")  # IPv4 or IPv6

    try:
        with open(log_path, 'r') as f:
            lines = f.readlines()

        parsed_data = []
        for line in lines:
            try:
                quoted = re.findall(r'"([^"]*)"', line)
                bracketed = re.findall(r'\[([^\]]+)\]', line)
                if len(quoted) < 4 or len(bracketed) < 1:
                    continue

                time_str = bracketed[0]
                request = quoted[0]
                referrer = quoted[1]
                user_agent = quoted[2]
                proxy_chain = quoted[3]

                status_match = re.search(r'"\s*(\d{3})\s+(\d+)\s', line)
                if status_match:
                    status = int(status_match.group(1))
                    size = int(status_match.group(2))
                else:
                    status = None
                    size = None

                req_time_match = re.search(r'" ([\d\.]+) "', line)
                req_time = req_time_match.group(1) if req_time_match else None

                method, path, protocol = ('', '', '')
                req_parts = request.split()
                if len(req_parts) == 3:
                    method, path, protocol = req_parts

                try:
                    time = datetime.strptime(time_str, '%d/%b/%Y:%H:%M:%S %z')
                except Exception:
                    time = None

                real_ip = proxy_chain.split(',')[0].strip() if proxy_chain else '-'
                ip = real_ip if ip_regex.match(real_ip) else '-'

                parsed_data.append({
                    'ip': ip,
                    'time': time,
                    'method': method,
                    'path': path,
                    'protocol': protocol,
                    'status': status,
                    'size': size,
                    'referrer': referrer,
                    'user_agent': user_agent,
                    'req_time': req_time,
                    'proxy_chain': proxy_chain
                })
            except Exception as e:
                continue
        df = pd.DataFrame(parsed_data)
        if not df.empty:
            df['status'] = pd.to_numeric(df['status'], errors='coerce')
        return df
    except Exception as e:
        st.warning(f"Couldn't parse {log_path}: {str(e)}")
        return pd.DataFrame()

def parse_php_error_log(log_path):
    # Regex to extract [timestamp] PHP <type>: <message>
    pattern = re.compile(r'\[(.*?)\]\s+PHP\s+([A-Za-z ]+):\s*(.*)')
    parsed = []
    with open(log_path, 'r') as f:
        for line in f:
            m = pattern.match(line)
            if m:
                time_str, error_type, message = m.groups()
                error_type = error_type.strip().lower()
                if error_type == "fatal error":
                    error_type_label = "Fatal Error (Critical)"
                elif error_type == "warning":
                    error_type_label = "Warning"
                elif error_type == "notice":
                    error_type_label = "Info"
                else:
                    error_type_label = error_type.capitalize()
                parsed.append({
                    'time': time_str,
                    'type': error_type_label,
                    'message': message
                })
    return pd.DataFrame(parsed)


def get_site_list():
    try:
        result = subprocess.run(
            ["terminus", "site:list", "--format=json"],
            capture_output=True, text=True, check=True
        )
        sites = json.loads(result.stdout)
        return sorted([v["name"] for v in sites.values()])
    except Exception as e:
        st.warning(f"Could not fetch site list: {e}")
        return []

def get_env_list(site_name):
    try:
        result = subprocess.run(
            ["terminus", "env:list", site_name, "--format=json"],
            capture_output=True, text=True, check=True
        )
        envs = json.loads(result.stdout)
        return sorted(envs.keys())
    except Exception as e:
        st.warning(f"Could not fetch environment list: {e}")
        return []

def get_site_uuid(site_name):
    try:
        result = subprocess.run(
            ["terminus", "site:list", "--format=json"],
            capture_output=True, text=True, check=True
        )
        sites = json.loads(result.stdout)
        return next((k for k, v in sites.items() if v["name"].lower() == site_name.lower()), None)
    except Exception as e:
        st.error(f"Error getting site UUID: {str(e)}")
        return None

def collect_logs(site_uuid, env, site_name):
    logs_dir = os.path.expanduser(f"~/site-logs/{site_name}_{env}")
    if os.path.exists(logs_dir):
        shutil.rmtree(logs_dir)
    os.makedirs(logs_dir, exist_ok=True)

    app_servers = subprocess.check_output(
        ["dig", "+short", "-4", f"appserver.{env}.{site_uuid}.drush.in"]
    ).decode().splitlines()

    for server in filter(None, app_servers):
        server_dir = os.path.join(logs_dir, f"app_server_{server}")
        os.makedirs(server_dir, exist_ok=True)
        for log, log_subdir in [("nginx-access.log", "nginx"), ("error.log", "nginx"), ("php-error.log", "php")]:
            cmd = (
                f'echo "get logs/{log_subdir}/{log}" | '
                f'sftp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o Port=2222 '
                f'{env}.{site_uuid}@{server}'
            )
            yield f"Connecting to {server} for {log}..."
            process = subprocess.Popen(cmd, shell=True, cwd=server_dir, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True)
            for line in process.stdout:
                if "Warning: Permanently added" in line:
                    continue
                if "Connected to" in line or "Fetching" in line or "sftp>" in line or log in line or "100%" in line:
                    yield f"[{server}] {line.strip()}"
            process.wait()
            yield f"[{server}] Finished {log}"

def detect_brute_force(df):
    brute_force_df = df[df['path'].str.contains('login', case=False) & df['status'].isin([401, 403])]
    brute_force_group = brute_force_df.groupby(['ip', 'path']).size().reset_index(name='count')
    return brute_force_group[brute_force_group['count'] > 5]

def detect_sql_injection(df):
    sql_keywords = ['SELECT', 'UNION', 'DROP', 'INSERT', 'UPDATE', 'DELETE', 'WHERE', 'OR', 'AND']
    df['potential_sql_injection'] = df['path'].str.contains('|'.join(sql_keywords), case=False, na=False) | \
                                    df['referrer'].str.contains('|'.join(sql_keywords), case=False, na=False)
    return df[df['potential_sql_injection']]

def detect_xss(df):
    xss_patterns = ['<script>', 'javascript:', 'onerror=', 'onload=']
    df['potential_xss'] = df['path'].str.contains('|'.join(xss_patterns), case=False, na=False) | \
                          df['referrer'].str.contains('|'.join(xss_patterns), case=False, na=False)
    return df[df['potential_xss']]

def generate_goaccess_report(log_path, output_path):
    try:
        cmd = f"goaccess {log_path} > {output_path}"
        subprocess.run(cmd, shell=True, check=True)
        st.success(f"Report generated successfully at {output_path}")
    except subprocess.CalledProcessError as e:
        st.error(f"Failed to generate report: {str(e)}")
    except Exception as e:
        st.error(f"An error occurred: {str(e)}")

st.set_page_config(layout="wide", page_title="Nginx Log Analyzer")
st.title("📊Nginx Log Analyzer")
st.markdown("""
<style>
    .stProgress > div > div > div > div {
        background-color: #1DA1F2;
    }
</style>
""", unsafe_allow_html=True)

logs_dir = None
if 'site_name' in st.session_state and 'env' in st.session_state:
    logs_dir = os.path.expanduser(f"~/site-logs/{st.session_state['site_name']}_{st.session_state['env']}")

with st.sidebar:
    st.header("Configuration")

    if "site_list" not in st.session_state:
        with st.spinner("Retrieving site names..."):
            st.session_state["site_list"] = get_site_list()
    site_list = st.session_state["site_list"]
    if site_list:
        site_name = st.selectbox("Site Name", site_list, key="site_name_select")
    else:
        site_name = st.text_input("Site Name (manual entry)")

    if site_name:
        env_cache_key = f"env_list_{site_name}"
        if env_cache_key not in st.session_state:
            with st.spinner("Retrieving environments..."):
                st.session_state[env_cache_key] = get_env_list(site_name)
        env_list = st.session_state[env_cache_key]
    else:
        env_list = []
    if env_list:
        env = st.selectbox("Environment", env_list, key="env_select")
    else:
        env = st.text_input("Environment (manual entry)")

    log_container = st.container()

    col1, col2 = st.columns(2)
    if col1.button("Collect Logs"):
        with st.spinner("Collecting logs..."):
            site_uuid = get_site_uuid(site_name)
            if site_uuid:
                log_lines = []
                with log_container:
                    st.markdown("#### Raw Output: Log Collecting")
                    log_progress = st.empty()
                    for output in collect_logs(site_uuid, env, site_name):
                        log_lines.append(output)
                        log_progress.code('\n'.join(log_lines[-10:]), language="bash")
                st.session_state['site_name'] = site_name
                st.session_state['env'] = env
                st.success("Log collection complete!")
            else:
                st.error("Invalid site name")

    if col2.button("Clear Logs"):
        logs_dir_temp = os.path.expanduser(f"~/site-logs")
        if os.path.exists(logs_dir_temp):
            try:
                shutil.rmtree(logs_dir_temp)
                st.success("Logs have been cleared successfully!")
                if 'site_name' in st.session_state:
                    del st.session_state['site_name']
                if 'env' in st.session_state:
                    del st.session_state['env']
            except Exception as e:
                st.error(f"Failed to clear logs: {str(e)}")
        else:
            st.warning("No logs directory found to clear.")

    if st.button("Generate Report"):
        if logs_dir and os.path.exists(logs_dir):
            combined_log_path = os.path.join(logs_dir, "combined_nginx_access.log")
            with open(combined_log_path, 'w') as outfile:
                for server_dir in os.listdir(logs_dir):
                    log_path = os.path.join(logs_dir, server_dir, "nginx-access.log")
                    if os.path.exists(log_path):
                        with open(log_path, 'r') as infile:
                            outfile.write(infile.read())

            report_path = os.path.join(logs_dir, "report.html")
            generate_goaccess_report(combined_log_path, report_path)

            if os.path.exists(report_path):
                with open(report_path, "rb") as file:
                    btn = st.download_button(
                        label="Download Report",
                        data=file,
                        file_name="report.html",
                        mime="text/html"
                    )
        else:
            st.warning("No logs directory found to generate report from.")

logs_dir = None
if 'site_name' in st.session_state and 'env' in st.session_state:
    logs_dir = os.path.expanduser(f"~/site-logs/{st.session_state['site_name']}_{st.session_state['env']}")

if logs_dir and os.path.exists(logs_dir):
    if 'site_name' in st.session_state and st.session_state['site_name']:
        env_display = st.session_state['env'] if 'env' in st.session_state else ''
        st.info(f"Currently displaying logs for: **{st.session_state['site_name']}** ({env_display})")
    tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8 = st.tabs(
        ["Overview", "Requests", "Errors", "Security", "File Types", "Bot & Crawler Detection", "Advanced Security", "PHP Errors"])

    all_logs = []
    php_error_logs = []
    for server_dir in os.listdir(logs_dir):
        log_path = os.path.join(logs_dir, server_dir, "nginx-access.log")
        if os.path.exists(log_path):
            df = parse_nginx_log(log_path)
            if not df.empty:
                df['server'] = server_dir
                all_logs.append(df)
        # Corrected path for PHP error log
        php_log_path = os.path.join(logs_dir, server_dir, "php-error.log")
        if os.path.exists(php_log_path):
            php_df = parse_php_error_log(php_log_path)
            if not php_df.empty:
                php_df['server'] = server_dir
                php_error_logs.append(php_df)

    # --- OVERVIEW TAB ---
    if all_logs:
        df = pd.concat(all_logs, ignore_index=True)
        df['status'] = pd.to_numeric(df['status'], errors='coerce')

        def get_extension(path):
            if not isinstance(path, str):
                return ""
            path = path.split('?', 1)[0]
            if '.' in path.split('/')[-1]:
                return path.split('.')[-1].lower()
            return ""

        df['extension'] = df['path'].apply(get_extension)

        with tab1:
            st.header("Traffic Overview")
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("Total Requests", len(df))
            with col2:
                st.metric("Unique IPs", df['ip'].nunique())
            with col3:
                st.metric("Error Rate", f"{len(df[df['status'] >= 400]) / len(df):.1%}")
            if not df['time'].isnull().all():
                time_df = df.set_index('time').resample('h').agg({
                    'ip': 'count',
                    'status': lambda x: (x >= 400).sum()
                }).rename(columns={'ip': 'requests', 'status': 'errors'})
                fig = px.area(time_df, x=time_df.index, y=['requests', 'errors'],
                              title="Requests Over Time")
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("No valid timestamps in logs for time series chart.")
            st.download_button(
                label="Download All Log Data as CSV",
                data=df.to_csv(index=False),
                file_name="nginx_logs.csv",
                mime="text/csv"
            )

        with tab2:
            st.header("Request Analysis")
            col1, col2 = st.columns(2)
            with col1:
                st.subheader("Top Paths")
                top_paths = df['path'].value_counts().head(10).reset_index()
                top_paths.columns = ['Path', 'Count']
                st.dataframe(top_paths, use_container_width=True)
            with col2:
                st.subheader("Status Codes")
                top_status = df['status'].value_counts().head(10).reset_index()
                top_status.columns = ['Status', 'Count']
                st.dataframe(top_status, use_container_width=True)
            st.subheader("User Agents")
            top_agents = df['user_agent'].value_counts().head(10).reset_index()
            top_agents.columns = ['User Agent', 'Count']
            st.dataframe(top_agents, use_container_width=True)
            st.subheader("Visitor Hostnames and IPs")
            top_ips = df['ip'].value_counts().head(10).reset_index()
            top_ips.columns = ['IP Address', 'Count']

            def resolve_hostname(ip):
                try:
                    import socket
                    return socket.gethostbyaddr(ip)[0]
                except Exception:
                    return "N/A"

            top_ips['Hostname'] = top_ips['IP Address'].apply(resolve_hostname)
            st.dataframe(top_ips[['IP Address', 'Hostname', 'Count']], use_container_width=True)
            st.subheader("Top Referrers")
            top_referrers = df['referrer'].value_counts().head(10).reset_index()
            top_referrers.columns = ['Referrer', 'Count']
            top_referrers['Referrer'] = top_referrers['Referrer'].replace('-', '[root]')
            st.dataframe(top_referrers, use_container_width=True)

        with tab3:
            st.header("Error Analysis")
            error_df = df[df['status'] >= 400]
            if not error_df.empty:
                display_cols = ['time', 'status', 'path', 'ip', 'referrer', 'user_agent']
                error_display = error_df[display_cols].sort_values('time', ascending=False).reset_index(drop=True)
                error_display.columns = ['Time', 'Status', 'Path', 'IP', 'Referrer', 'User Agent']
                st.dataframe(error_display, use_container_width=True)
            else:
                st.info("No errors found in logs")

        with tab4:
            st.header("Suspicious Activity Detection")
            error_stats = df.groupby('ip').agg(
                total_requests=('status', 'count'),
                error_requests=('status', lambda x: (x >= 400).sum())
            )
            error_stats['error_rate'] = error_stats['error_requests'] / error_stats['total_requests']
            high_error_ips = error_stats[(error_stats['error_rate'] > 0.5) & (error_stats['total_requests'] > 10)]
            high_error_ips = high_error_ips.sort_values('error_rate', ascending=False).head(10)
            high_error_ips['error_rate'] = (high_error_ips['error_rate'] * 100).round(1).astype(str) + '%'
            high_error_ips = high_error_ips.reset_index()
            high_error_ips.columns = ['IP Address', 'Total Requests', 'Error Requests', 'Error Rate']
            st.subheader("IPs with High Error Rate (>50%)")
            st.dataframe(high_error_ips, use_container_width=True)

            notfound_ips = df[df['status'] == 404]['ip'].value_counts().head(10).reset_index()
            notfound_ips.columns = ['IP Address', '404 Count']
            st.subheader("IPs with Most 404s")
            st.dataframe(notfound_ips, use_container_width=True)
            top_request_ips = df['ip'].value_counts().head(10).reset_index()
            top_request_ips.columns = ['IP Address', 'Request Count']
            st.subheader("Top Requesting IPs")
            st.dataframe(top_request_ips, use_container_width=True)

        with tab5:
            st.header("File Types Analysis")
            st.subheader("Top Requested File Extensions")
            top_ext = df['extension'].value_counts().head(10).reset_index()
            top_ext.columns = ['Extension', 'Count']
            top_ext['Extension'] = top_ext['Extension'].replace('', '[root]')
            st.dataframe(top_ext, use_container_width=True)

            st.subheader("Top Requested Files")
            top_files = df['path'].value_counts().head(10).reset_index()
            top_files.columns = ['File Path', 'Count']
            st.dataframe(top_files, use_container_width=True)

            st.subheader("File Extension Distribution (Bar Chart)")
            fig_bar = px.bar(top_ext, x='Extension', y='Count', title="Top Requested File Extensions")
            st.plotly_chart(fig_bar, use_container_width=True)

            st.subheader("File Extension Distribution (Pie Chart)")
            fig_pie = px.pie(top_ext, names='Extension', values='Count',
                             title="Top Requested File Extensions (Pie)")
            st.plotly_chart(fig_pie, use_container_width=True)

            st.markdown("""
                **Details:**
                - The table above shows the most frequently requested file extensions (e.g., html, php, js, css, jpg, png).
                - The bar and pie charts visualize the distribution of file types.
                - The "Top Requested Files" table shows the most accessed individual files/paths.
                """)

        with tab6:
            st.header("Bot & Crawler Detection")
            bot_keywords = [
                "bot", "spider", "crawl", "slurp", "baidu", "bingpreview", "duckduckbot", "yandex", "sogou",
                "exabot",
                "facebot", "ia_archiver", "mj12bot", "ahrefsbot", "semrushbot", "dotbot", "gigabot", "seznambot",
                "panscient",
                "applebot", "petalbot", "gptbot", "python-requests", "curl", "wget"
            ]
            df['is_bot'] = df['user_agent'].str.lower().str.contains('|'.join(bot_keywords), na=False)
            bots_df = df[df['is_bot']]
            if not bots_df.empty:
                st.subheader("Top Bots & Crawlers by Request Count")
                top_bots = bots_df['user_agent'].value_counts().head(10).reset_index()
                top_bots.columns = ['User Agent', 'Request Count']
                st.dataframe(top_bots, use_container_width=True)

                st.subheader("Bot/Crawler Activity by Path")
                bot_paths = bots_df['path'].value_counts().head(10).reset_index()
                bot_paths.columns = ['Path', 'Request Count']
                st.dataframe(bot_paths, use_container_width=True)

                st.subheader("Bot/Crawler Error Rate")
                bot_error_rate = (bots_df['status'] >= 400).mean()
                st.metric("Bot/Crawler Error Rate", f"{bot_error_rate:.1%}")

                st.subheader("All Bot/Crawler Requests (sample output)")
                sample_bot_data = bots_df[['time', 'ip', 'path', 'status', 'user_agent']].head(50)
                st.dataframe(sample_bot_data, use_container_width=True)

                csv_bot_data = bots_df[['time', 'ip', 'path', 'status', 'user_agent']].to_csv(index=False)
                st.download_button(
                    label="Download Full Bot/Crawler Data as CSV",
                    data=csv_bot_data,
                    file_name="bot_crawler_requests.csv",
                    mime="text/csv"
                )
                st.caption("Showing a sample of 50 rows above. Download for the full dataset.")
            else:
                st.info("No bots or crawlers detected in the logs.")

        with tab7:
            st.header("Advanced Security Analysis")

            st.subheader("Potential Brute Force Attacks")
            brute_force_suspects = detect_brute_force(df)
            if not brute_force_suspects.empty:
                st.dataframe(brute_force_suspects, use_container_width=True)
            else:
                st.info("No potential brute force attacks detected.")

            st.subheader("Potential SQL Injection Attempts")
            sql_injection_df = detect_sql_injection(df)
            if not sql_injection_df.empty:
                st.dataframe(sql_injection_df[['time', 'ip', 'path', 'referrer']], use_container_width=True)
            else:
                st.info("No potential SQL injection attempts detected.")

            st.subheader("Potential XSS Attacks")
            xss_df = detect_xss(df)
            if not xss_df.empty:
                st.dataframe(xss_df[['time', 'ip', 'path', 'referrer']], use_container_width=True)
            else:
                st.info("No potential XSS attacks detected.")

    with tab8:
        st.header("PHP Errors")
        if php_error_logs:
            php_df = pd.concat(php_error_logs, ignore_index=True)
            error_type_options = ["All", "Fatal Error (Critical)", "Warning", "Info"]
            error_type = st.selectbox("Show only...", error_type_options, key="php_error_type")
            if error_type != "All":
                filtered_df = php_df[php_df['type'] == error_type]
            else:
                filtered_df = php_df
            st.dataframe(filtered_df, use_container_width=True)
            st.download_button(
                label="Download PHP Error Log as CSV",
                data=filtered_df.to_csv(index=False),
                file_name="php_error_log.csv",
                mime="text/csv"
            )
        else:
            st.info("No PHP errors found.")
else:
    st.info("Select site and env to begin analysis")
