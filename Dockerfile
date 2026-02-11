# Use an official Python runtime as a parent image
FROM python:3.9-slim

# Set the working directory in the container
WORKDIR /app

# Copy the current directory contents into the container at /app
COPY . /app

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Set environment variables (optional, you can set them during runtime)
ENV API_KEY 069e4f0b7cb9ca4b263aa518598bac77
ENV SECRET_KEY 5b28c9a74449561dd3351943c7e521d2
ENV PI42_API_KEY 069e4f0b7cb9ca4b263aa518598bac77
ENV PI42_API_SECRET 5b28c9a74449561dd3351943c7e521d2

# Run the bot
CMD ["python3", "run.py"]
