import json

print('Scan results:')
with open('scan_results.json') as f:
    data = json.load(f)
    print(f'Found {len(data)} stocks')
    print(f'First stock: {data[0]["ticker"]} - ${data[0]["price"]}')

print('\nAnalysis results:')
with open('analysis_results.json') as f:
    data = json.load(f)
    print(f'Found {len(data)} stocks with analysis')
    print(f'Top stock: {data[0]["ticker"]} - Score {data[0]["analysis"]["score"]}/10')