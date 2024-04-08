#
# gpt_assistant.py
#
# Assistant implementation based on OpenAI's GPT models. This assistant is capable of leveraging
# separate web search and vision tools.
#

#
# TODO:
# -----
# - Move to streaming completions and detect timeouts when a threshold duration elapses since the
#   the last token was emitted.
# - Figure out how to get assistant to stop referring to "photo" and "image" when analyzing photos.
# - Improve people search.
#

import asyncio
import json
import timeit
from typing import Any, Dict, List

import openai
from openai.types.chat import ChatCompletionMessageToolCall

from .assistant import Assistant, AssistantResponse
from web_search import WebSearch, WebSearchResult
from vision import Vision
from models import Role, Message, Capability, TokenUsage, accumulate_token_usage


####################################################################################################
# Prompts
####################################################################################################

#
# Top-level instructions
#

SYSTEM_MESSAGE = """
You are Noa, a smart personal AI assistant inside the user's AR smart glasses that answers all user
queries and questions. You have access to a photo from the smart glasses camera of what the user was
seeing at the time they spoke.

Make your responses short (one or two sentences) and precise. Respond without any preamble when giving
translations, just translate directly. When analyzing the user's view, speak as if you can actually
see and never make references to the photo or image you analyzed.
"""

#
# Vision tool instructions
#

VISION_PHOTO_DESCRIPTION_SYSTEM_MESSAGE = """
You are Noa, a smart personal AI assistant inside the user's AR smart glasses that answers all user
queries and questions. You have access to a photo from the smart glasses camera of what the user was
seeing at the time they spoke but you NEVER mention the photo or image and instead respond as if you
are actually seeing.

The camera is unfortunately VERY low quality but the user is counting on you to interpret the
blurry, pixelated images. NEVER comment on image quality. Do your best with images.

Make your responses short (one or two sentences) and precise. Respond without any preamble when giving
translations, just translate directly. When analyzing the user's view, speak as if you can actually
see and never make references to the photo or image you analyzed.
"""

VISION_GENERATE_SEARCH_DESCRIPTION_FROM_PHOTO_SYSTEM_MESSAGE = """
you are photo tool, with help of photo and user's query, make a short (1 SENTENCE) and concise google search query that can be searched on internet to answer the user.
"""

VISION_GENERATE_REVERSE_IMAGE_SEARCH_QUERY_FROM_PHOTO_SYSTEM_MESSAGE = """
you are photo tool, with help of photo and user's query, make a short (1 SENTENCE) and concise google search query that can be searched on internet with google reverse image search to answer the user.
"""

#
# Learned Context
#
# Information about the user can be extracted by analyzing batches of their messages and turned into
# a simple list of key-value pairs. Feeding these back to the assistant will produce more relevant,
# contextually-aware, and personalized responses.
#

# These are context keys we try to detect in conversation history over time
LEARNED_CONTEXT_KEY_DESCRIPTIONS = {
    "UserName": "User's name",
    "DOB": "User's date of birth",
    "Food": "Foods and drinks user has expressed interest in"
}

LEARNED_CONTEXT_EXTRACTION_SYSTEM_MESSAGE = f"""
Given a transcript of what the user said, look for any of the following information being revealed:

""" + "\n".join([ key + ": "  + description for key, description in LEARNED_CONTEXT_KEY_DESCRIPTIONS.items() ]) + """

Make sure to list them in this format:

KEY=VALUE

If nothing was found, just say "END". ONLY PRODUCE ITEMS WHEN THE USER HAS ACTUALLY REVEALED THEM.
"""

CONTEXT_SYSTEM_MESSAGE_PREFIX = "## Additional context about the user:"


####################################################################################################
# Tools
####################################################################################################

DUMMY_SEARCH_TOOL_NAME = "general_knowledge_search"
SEARCH_TOOL_NAME = "search"
PHOTO_TOOL_NAME = "analyze_photo"
QUERY_PARAM_NAME = "query"
PHOTO_TOOL_WEB_SEARCH_PARAM_NAME = "google_reverse_image_search"
PHOTO_TOOL_TRANSLATION_PARAM_NAME = "translate"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": DUMMY_SEARCH_TOOL_NAME,
            "description": """Trivial and general knowledge that would be expected to exist in Wikipedia or an encyclopedia""",
            "parameters": {
                "type": "object",
                "properties": {
                    QUERY_PARAM_NAME: {
                        "type": "string",
                        "description": "search query",
                    },
                },
                "required": [ QUERY_PARAM_NAME ]
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": SEARCH_TOOL_NAME,
            "description": """Provides up to date information on news, retail products, current events, and esoteric knowledge""",
            "parameters": {
                "type": "object",
                "properties": {
                    QUERY_PARAM_NAME: {
                        "type": "string",
                        "description": "search query",
                    },
                },
                "required": [ QUERY_PARAM_NAME ]
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": PHOTO_TOOL_NAME,
            "description": """Analyzes or describes the photo you have from the user's current perspective.
Use this tool if user refers to something not identifiable from conversation context, such as with a demonstrative pronoun.""",
            "parameters": {
                "type": "object",
                "properties": {
                    QUERY_PARAM_NAME: {
                        "type": "string",
                        "description": "User's query to answer, describing what they want answered, expressed as a command that NEVER refers to the photo or image itself"
                    },
                    PHOTO_TOOL_WEB_SEARCH_PARAM_NAME: {
                        "type": "boolean",
                        "description": "True ONLY if user wants to look up facts about contents of photo online (simply identifying what is in the photo does not count), otherwise always false"
                    },
                    PHOTO_TOOL_TRANSLATION_PARAM_NAME: {
                        "type": "boolean",
                        "description": "Translation of something in user's view required"
                    }
                },
                "required": [ QUERY_PARAM_NAME, PHOTO_TOOL_WEB_SEARCH_PARAM_NAME, PHOTO_TOOL_TRANSLATION_PARAM_NAME ]
            },
        },
    },
]

async def handle_tool(
    tool_call: ChatCompletionMessageToolCall,
    user_message: str,
    image_bytes: bytes | None,
    location: str | None,
    local_time: str | None,
    web_search: WebSearch,
    vision: Vision,
    learned_context: Dict[str, str] | None,
    token_usage_by_model: Dict[str, TokenUsage],
    capabilities_used: List[Capability],
    tools_used: List[Dict[str, Any]]
) -> str:
    tool_functions = {
        SEARCH_TOOL_NAME: web_search.search_web,                # returns WebSearchResult
        PHOTO_TOOL_NAME: handle_photo_tool,                     # returns WebSearchResult | str
        DUMMY_SEARCH_TOOL_NAME: handle_general_knowledge_tool,  # returns str
    }

    function_name = tool_call.function.name
    function_to_call = tool_functions.get(function_name)
    if function_to_call is None:
        # Error: GPT hallucinated a tool
        return "Error: you hallucinated a tool that doesn't exist. Tell user you had trouble interpreting the request and ask them to rephrase it."

    function_args = prepare_tool_arguments(
        tool_call=tool_call,
        user_message=user_message,
        image_bytes=image_bytes,
        location=location,
        local_time=local_time,
        web_search=web_search,
        vision=vision,
        learned_context=learned_context,
        token_usage_by_model=token_usage_by_model,
        capabilities_used=capabilities_used
    )

    tool_start_time = timeit.default_timer()
    function_response: WebSearchResult | str = await function_to_call(**function_args)
    total_tool_time = round(timeit.default_timer() - tool_start_time, 3)

    # Record capability used (except for case of photo tool, which reports on its own because it
    # can invoke multiple capabilities)
    if function_name == SEARCH_TOOL_NAME:
        capabilities_used.append(Capability.WEB_SEARCH)
    elif function_name == DUMMY_SEARCH_TOOL_NAME:
        capabilities_used.append(Capability.ASSISTANT_KNOWLEDGE)

    tools_used.append(
        create_debug_tool_info_object(
            function_name=function_name,
            function_args=function_args,
            tool_time=total_tool_time,
            search_result=function_response.search_provider_metadata if isinstance(function_response, WebSearchResult) else None
        )
    )

    # Format response appropriately
    assert isinstance(function_response, WebSearchResult) or isinstance(function_response, str)
    tool_output = function_response.summary if isinstance(function_response, WebSearchResult) else function_response
    return tool_output

def prepare_tool_arguments(
    tool_call: ChatCompletionMessageToolCall,
    user_message: str,
    image_bytes: bytes | None,
    location: str | None,
    local_time: str | None,
    web_search: WebSearch,
    vision: Vision,
    learned_context: Dict[str, str] | None,
    token_usage_by_model: Dict[str, TokenUsage],
    capabilities_used: List[Capability]
) -> Dict[str, Any]:
    # Get function description we passed to GPT. This function should be called after we have
    # validated that a valid tool call was generated.
    function_description = [ description for description in TOOLS if description["function"]["name"] == tool_call.function.name ][0]
    function_parameters = function_description["function"]["parameters"]["properties"]

    # Parse arguments and ensure they are all str or bool for now. Drop any that aren't.
    args: Dict[str, Any] = {}
    try:
        args = json.loads(tool_call.function.arguments)
    except:
        pass
    for param_name in list(args.keys()):
        if param_name not in function_parameters:
            # GPT hallucinated a parameter
            del args[param_name]
            continue
        if function_parameters[param_name]["type"] == "string" and type(args[param_name]) != str:
            del args[param_name]
            continue
        if function_parameters[param_name]["type"] == "boolean" and type(args[param_name]) != bool:
            del args[param_name]
            continue
        if function_parameters[param_name]["type"] not in [ "string", "boolean" ]:
            # Need to keep this up to date with the tools we define
            raise ValueError(f"Unsupported tool parameter type: {function_parameters[param_name]['type']}")

    # Fill in args required by all tools
    args["location"] = location if location else "unknown"
    args[QUERY_PARAM_NAME] = args[QUERY_PARAM_NAME] if QUERY_PARAM_NAME in args else user_message

    # Photo tool additional parameters we need to inject
    if tool_call.function.name == PHOTO_TOOL_NAME:
        args["image_bytes"] = image_bytes
        args["vision"] = vision
        args["web_search"] = web_search
        args["local_time"] = local_time
        args["learned_context"] = learned_context
        args["token_usage_by_model"] = token_usage_by_model
        args["capabilities_used"] = capabilities_used

    return args

async def handle_general_knowledge_tool(
    query: str,
    image_bytes: bytes | None = None,
    local_time: str | None = None,
    location: str | None = None,
    learned_context: Dict[str,str] | None = None,
) -> str:
    """
    Dummy general knowledge tool that tricks GPT into generating an answer directly instead of
    reaching for web search. GPT knows that the web contains information on virtually everything, so
    it tends to overuse web search. One solution is to very carefully enumerate the cases for which
    web search is appropriate, but this is tricky. Should "Albert Einstein's birthday" require a web
    search? Probably not, as GPT has this knowledge baked in. The trick we use here is to create a
    "general knowledge" tool that contains any information Wikipedia or an encyclopedia would have
    (a reasonable proxy for things GPT knows). We return an empty string, which forces GPT to
    produce its own response at the expense of a little bit of latency for the tool call.
    """
    return ""

@staticmethod
async def handle_photo_tool(
    query: str,
    vision: Vision,
    web_search: WebSearch,
    token_usage_by_model: Dict[str, TokenUsage],
    capabilities_used: List[Capability],
    google_reverse_image_search: bool = False,  # default in case GPT doesn't generate it
    translate: bool = False,                    # default in case GPT doesn't generate it
    image_bytes: bytes | None = None,
    local_time: str | None = None,
    location: str | None = None,
    learned_context: Dict[str,str] | None = None
) -> str | WebSearchResult:
    extra_context = "\n\n" + GPTAssistant._create_context_system_message(local_time=local_time, location=location, learned_context=learned_context)

    # If no image bytes (glasses always send image but web playgrounds do not), return an error
    # message for the assistant to use
    if image_bytes is None or len(image_bytes) == 0:
        # Because this is a tool response, using "tell user" seems to ensure that the final
        # assistant response is what we want
        return "Error: no photo supplied. Tell user: I think you're referring to something you can see. Can you provide a photo?"

    # Reverse image search? Use vision tool -> search query, then search.
    # Translation special case: never use reverse image search for it.
    # NOTE: We do not pass history for now but maybe we should in some cases?
    if google_reverse_image_search and not translate:
        capabilities_used.append(Capability.REVERSE_IMAGE_SEARCH)
        system_prompt = VISION_GENERATE_REVERSE_IMAGE_SEARCH_QUERY_FROM_PHOTO_SYSTEM_MESSAGE + extra_context
        vision_response = await vision.query_image(
            system_message=system_prompt,
            query=query,
            image_bytes=image_bytes,
            token_usage_by_model=token_usage_by_model
        )
        return await web_search.search_web(query=vision_response.strip("\""), use_photo=True, image_bytes=image_bytes, location=location)

    # Just use vision tool
    capabilities_used.append(Capability.VISION)
    system_prompt = VISION_PHOTO_DESCRIPTION_SYSTEM_MESSAGE + extra_context
    response = await vision.query_image(
        system_message=system_prompt,
        query=query,
        image_bytes=image_bytes,
        token_usage_by_model=token_usage_by_model
    )
    print(f"vision: {response}")
    return response

def create_debug_tool_info_object(function_name: str, function_args: Dict[str, Any], tool_time: float, search_result: str | None = None) -> Dict[str, Any]:
    """
    Produces an object of arbitrary keys and values intended to serve as a debug description of tool
    use.
    """
    function_args = function_args.copy()

    # Sanitize bytes, which are often too long to print
    for arg_name, value in function_args.items():
        if isinstance(value, bytes):
            function_args[arg_name] = "<bytes>"
        if isinstance(value, list):
            function_args[arg_name] = ", ".join(function_args[arg_name])
    if "vision" in function_args:
        del function_args["vision"]
    if "web_search" in function_args:
        del function_args["web_search"]
    if "token_usage_by_model" in function_args:
        del function_args["token_usage_by_model"]
    if "prompt" in function_args:
        del function_args["prompt"]
    to_return = {
        "tool": function_name,
        "tool_args": function_args,
        "tool_time": tool_time
    }
    if search_result:
        to_return["search_result"] = search_result
    return to_return

@staticmethod
def create_hallucinated_tool_info_object(function_name: str) -> Dict[str, str]:
    return { "tool": function_name, "hallucinated": "true" }


####################################################################################################
# Assistant Class
####################################################################################################

class GPTAssistant(Assistant):
    def __init__(self, client: openai.AsyncOpenAI):
        self._client = client

    # Refer to definition of Assistant for description of parameters
    async def send_to_assistant(
        self,
        prompt: str,
        image_bytes: bytes | None,
        message_history: List[Message] | None,
        location_address: str | None,
        local_time: str | None,
        model: str | None,
        web_search: WebSearch,
        vision: Vision
    ) -> AssistantResponse:
        start = timeit.default_timer()

        # Default model
        model = model if model is not None else "gpt-3.5-turbo-1106"

        # Prepare response datastructure
        returned_response = AssistantResponse(token_usage_by_model={}, capabilities_used=[], response="", debug_tools="")

        # Make copy of message history so we can modify it in-flight during tool use
        message_history = message_history.copy() if message_history else None

        # Add user message to message history or create a new one if necessary
        user_message = Message(role=Role.USER, content=prompt)
        system_message = Message(role=Role.SYSTEM, content=SYSTEM_MESSAGE)
        if not message_history:
            message_history = []
        if len(message_history) == 0:
            message_history = [ system_message ]
        else:
            # Insert system message before message history, unless client transmitted one they want
            # to use
            if len(message_history) > 0 and message_history[0].role != Role.SYSTEM:
                message_history.insert(0, system_message)
        message_history.append(user_message)
        message_history = self._prune_history(message_history=message_history)

        # Update learned context by analyzing last N messages.
        # TODO: this was for demo purposes and needs to be made more robust. Should be triggered
        #       periodically or when user asks something for which context is needed.
        #learned_context.update(await self._extract_learned_context(message_history=message_history, model=model, token_usage_by_model=returned_response.token_usage_by_model))
        learned_context = {}

        # Inject context into our copy by appending it to system message. Unclear whether multiple
        # system messages are confusing to the assistant or not but cursory testing shows this
        # seems to work.
        extra_context_message = Message(role=Role.SYSTEM, content=self._create_context_system_message(local_time=local_time, location=location_address, learned_context=learned_context))
        message_history.append(extra_context_message)

        # Initial GPT call, which may request tool use
        first_response = await self._client.chat.completions.create(
            model=model,
            messages=message_history,
            tools=TOOLS,
            tool_choice="auto"
        )
        first_response_message = first_response.choices[0].message

        # Aggregate token counts and potential initial response
        accumulate_token_usage(
            token_usage_by_model=returned_response.token_usage_by_model,
            model=model,
            input_tokens=first_response.usage.prompt_tokens,
            output_tokens=first_response.usage.completion_tokens,
            total_tokens=first_response.usage.total_tokens
        )

        # If there are no tool requests, the initial response will be returned
        returned_response.response = first_response_message.content

        # Handle tool requests
        tools_used = []
        tools_used.append({ "learned_context": learned_context })   # log context here for now
        if first_response_message.tool_calls:
            # Append initial response to history, which may include tool use
            message_history.append(first_response_message)

            # Invoke all the tools in parallel and wait for them all to complete
            tool_handlers = []
            for tool_call in first_response_message.tool_calls:
                tool_handlers.append(
                    handle_tool(
                        tool_call=tool_call,
                        user_message=prompt,
                        image_bytes=image_bytes,
                        location=location_address,
                        local_time=local_time,
                        web_search=web_search,
                        vision=vision,
                        learned_context=learned_context,
                        token_usage_by_model=returned_response.token_usage_by_model,
                        capabilities_used=returned_response.capabilities_used,
                        tools_used=tools_used
                    )
                )
            tool_outputs = await asyncio.gather(*tool_handlers)

            # Append all the responses for GPT to continue
            for i in range(len(tool_outputs)):
                message_history.append(
                    {
                        "tool_call_id": first_response_message.tool_calls[i].id,
                        "role": "tool",
                        "name": first_response_message.tool_calls[i].function.name,
                        "content": tool_outputs[i],
                    }
                )

            # Get final response from model
            second_response = await self._client.chat.completions.create(
                model=model,
                messages=message_history
            )

            # Aggregate tokens and response
            accumulate_token_usage(
                token_usage_by_model=returned_response.token_usage_by_model,
                model=model,
                input_tokens=second_response.usage.prompt_tokens,
                output_tokens=second_response.usage.completion_tokens,
                total_tokens=second_response.usage.total_tokens
            )
            returned_response.response = second_response.choices[0].message.content

        # If no tools were used, only assistant capability recorded
        if len(returned_response.capabilities_used) == 0:
            returned_response.capabilities_used.append(Capability.ASSISTANT_KNOWLEDGE)

        # Return final response
        returned_response.debug_tools = json.dumps(tools_used)
        stop = timeit.default_timer()
        print(f"Time taken: {stop-start:.3f}")

        return returned_response

    @staticmethod
    def _prune_history(message_history: List[Message]) -> List[Message]:
        """
        Prunes down the chat history to save tokens, improving inference speed and reducing cost.
        Generally, preserving all assistant responses is not needed, and only a limited number of
        user messages suffice to maintain a coherent conversation.

        Parameters
        ----------
        message_history : List[Message]
            Conversation history. This list will be mutated and returned.

        Returns
        -------
        List[Message]
            Pruned history. This is the same list passed as input.
        """
        # Limit to most recent 5 user messages and 3 assistant responses
        assistant_messages_remaining = 3
        user_messages_remaining = 5
        message_history.reverse()
        i = 0
        while i < len(message_history):
            if message_history[i].role == Role.ASSISTANT:
                if assistant_messages_remaining == 0:
                    del message_history[i]
                else:
                    assistant_messages_remaining -= 1
                    i += 1
            elif message_history[i].role == Role.USER:
                if user_messages_remaining == 0:
                    del message_history[i]
                else:
                    user_messages_remaining -= 1
                    i += 1
            else:
                i += 1
        message_history.reverse()
        return message_history

    @staticmethod
    def _create_context_system_message(local_time: str | None, location: str | None, learned_context: Dict[str,str] | None) -> str:
        """
        Creates a string of additional context that can either be appended to the main system
        message or as a secondary system message before delivering the assistant response. This is
        how GPT is made aware of the user's location, local time, and any learned information that
        was extracted from prior conversation.

        Parameters
        ----------
        local_time : str | None
            Local time, if known.
        location : str | None
            Location, as a human readable address, if known.
        learned_context : Dict[str,str] | None
            Information learned from prior conversation as key-value pairs, if any.

        Returns
        -------
        str
            Message to combine with existing system message or to inject as a new, extra system
            message.
        """
        # Fixed context: things we know and need not extract from user conversation history
        context: Dict[str, str] = {}
        if local_time is not None and len(local_time) > 0:
            context["current_time"] = local_time
        else:
            context["current_time"] = "If asked, tell user you don't know current date or time because clock is broken"
        if location is not None and len(location) > 0:
            context["location"] = location
        else:
            context["location"] = "You do not know user's location and if asked, tell them so"

        # Merge in learned context
        if learned_context is not None:
            context.update(learned_context)

        # Convert to a list to be appended to a system message or treated as a new system message
        system_message_fragment = CONTEXT_SYSTEM_MESSAGE_PREFIX + "\n".join([ f"<{key}>{value}</{key}>" for key, value in context.items() if value is not None ])
        return system_message_fragment

    async def _extract_learned_context(self, message_history: List[Message], model: str, token_usage_by_model: Dict[str, TokenUsage]) -> Dict[str,str]:
        # Grab last N user messages
        max_user_history = 2
        messages: List[Message] = []
        for i in range(len(message_history) - 1, -1, -1):
            if len(messages) >= max_user_history:
                break
            if message_history[i].role == Role.USER:
                messages.append(message_history[i])

        # Insert system message and reverse so that it is in the right order
        messages.append(Message(role=Role.SYSTEM, content=LEARNED_CONTEXT_EXTRACTION_SYSTEM_MESSAGE))
        messages.reverse()
        # print("Context extraction input:")
        # print(messages)

        # Process
        response = await self._client.chat.completions.create(
            model=model,
            messages=messages
        )

        # Do not forget to count tokens used!
        accumulate_token_usage(
            token_usage_by_model=token_usage_by_model,
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
            total_tokens=response.usage.total_tokens
        )

        # # Debug: print raw output of context extraction
        # print("Learned context:")
        # print(response.choices[0].message.content)

        # Parse it into a dictionary
        learned_context: Dict[str,str] = {}
        lines = response.choices[0].message.content.splitlines()
        for line in lines:
            parts = line.split("=")
            if len(parts) == 2:
                key, value = parts
                if key in LEARNED_CONTEXT_KEY_DESCRIPTIONS:
                    learned_context[key] = value
        return learned_context

Assistant.register(GPTAssistant)