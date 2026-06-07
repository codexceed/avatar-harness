from openai import OpenAI

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key="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",
)

response = client.responses.create(
    model="gpt-5.4",
    instructions="You are a coding assistant that talks like a pirate.",
    input="How do I check if a Python object is an instance of a class?",
)

print(response.output_text)
