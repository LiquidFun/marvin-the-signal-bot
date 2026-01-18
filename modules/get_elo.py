import requests
from bs4 import BeautifulSoup

def get_elo(url, group_id, username, password):
    with requests.Session() as s:
        data = {
            "group": group_id,
            "user": username,
            "pwd": password,
            "login": "Anmelden",
        }
        URL = url
        resp = s.post(URL + "index.jsp", data, verify=False)
        resp.raise_for_status()
        resp = s.get(URL + "stats.jsp?", verify=False)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, features="html.parser")
        stats_tbody = soup.find('div', class_='tbody stats')

        result = {}

        if stats_tbody:
            table = stats_tbody.find('table')
            rows = table.find_all('tr')
            
            # Process rows in pairs (each user has 2 rows)
            i = 0
            while i < len(rows):
                # First row contains user image and percentages
                row1 = rows[i]
                
                # Find user name from alt attribute
                img = row1.find('img', alt=True)
                if img:
                    username = img['alt']
                    
                    # Extract percentage scores from first row
                    scores = row1.find_all('td', class_='score')
                    percentages = [score.get_text(strip=True) for score in scores]
                    
                    # Second row contains Elo ratings
                    if i + 1 < len(rows):
                        row2 = rows[i + 1]
                        elos = row2.find_all('td', class_='elo')
                        elo_values = [elo.get_text(strip=True) for elo in elos]
                        
                        # Store in result
                        result[username] = {
                            'percentages': percentages,
                            'elos': elo_values
                        }
                
                i += 2 
        return result


if __name__ == "__main__":
    print(get_elo())
