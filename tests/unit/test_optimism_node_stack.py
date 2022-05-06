import aws_cdk as core
import aws_cdk.assertions as assertions

from optimism_node.optimism_node_stack import OptimismNodeStack

# example tests. To run these tests, uncomment this file along with the example
# resource in optimism_node/optimism_node_stack.py
def test_sqs_queue_created():
    app = core.App()
    stack = OptimismNodeStack(app, "optimism-node")
    template = assertions.Template.from_stack(stack)

#     template.has_resource_properties("AWS::SQS::Queue", {
#         "VisibilityTimeout": 300
#     })
