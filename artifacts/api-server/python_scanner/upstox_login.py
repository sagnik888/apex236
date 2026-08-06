import os
import requests
import json
import urllib.parse
from dotenv import dotenv_values

def main():
    env_path = "upstox_secrets.env"
    env_vars = dotenv_values(env_path)
    
    api_key = env_vars.get("UPSTOX_API_KEY")
    api_secret = env_vars.get("UPSTOX_API_SECRET")
    redirect_uri = env_vars.get("UPSTOX_REDIRECT_URI")
    
    if not api_key or not api_secret:
        print("Error: API Key or Secret not found in upstox_secrets.env")
        return
        
    params = {
        "response_type": "code",
        "client_id": api_key,
        "redirect_uri": redirect_uri
    }
    
    url = f"https://api.upstox.com/v2/login/authorization/dialog?{urllib.parse.urlencode(params)}"
    
    print("======================================================")
    print("Please go to the following URL in your browser to login:")
    print(url)
    print("======================================================")
    
    print("\nAfter logging in, you will be redirected to a URL that looks like:")
    print(f"{redirect_uri}?code=XXXXXXX")
    
    code = input("\nEnter the 'code' from the redirected URL: ").strip()
    
    if not code:
        print("No code provided. Exiting.")
        return
        
    print("\nExchanging code for access token...")
    
    token_url = "https://api.upstox.com/v2/login/authorization/token"
    headers = {
        "accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = {
        "code": code,
        "client_id": api_key,
        "client_secret": api_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code"
    }
    
    response = requests.post(token_url, headers=headers, data=data)
    
    if response.status_code == 200:
        result = response.json()
        access_token = result.get("access_token")
        if access_token:
            print(f"Successfully obtained access token!")
            
            # Update the upstox_secrets.env file
            with open(env_path, 'r') as f:
                lines = f.readlines()
                
            has_token = False
            with open(env_path, 'w') as f:
                for line in lines:
                    if line.startswith("UPSTOX_ACCESS_TOKEN="):
                        f.write(f'UPSTOX_ACCESS_TOKEN="{access_token}"\n')
                        has_token = True
                    else:
                        f.write(line)
                
                if not has_token:
                    if not lines[-1].endswith('\n'):
                        f.write('\n')
                    f.write(f'UPSTOX_ACCESS_TOKEN="{access_token}"\n')
                    
            print(f"Updated {env_path} with the new UPSTOX_ACCESS_TOKEN")
        else:
            print("Failed to get access_token from response:", result)
    else:
        print(f"Error ({response.status_code}):", response.text)

if __name__ == "__main__":
    main()
