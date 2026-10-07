# # app/gemini.py
# #full functional code is coming soon
# #  will be used for analyzing incidents and generating reports using Gemini API
# import os
# from google import genai

# client = genai.Client(
#     api_key=os.getenv("GEMINI_API_KEY")
# )

# def analyze_incident(incident_data):
#     prompt = f"""
#     Analyze this surveillance incident:

#     {incident_data}

#     Determine:
#     1. What happened?
#     2. Is it suspicious?
#     3. Severity level
#     4. Recommended action
#     """

#     response = client.models.generate_content(
#         model="gemini-2.5-flash",
#         contents=prompt
#     )

#     return response.text