// Host eligibility is not utilization. A visible ANE still needs Instruments to
// verify the actual execution path and power consumption on a physical Mac.
import CoreML
import Foundation

let devices = MLModel.availableComputeDevices
let hasANE = devices.contains { device in
    if case .neuralEngine = device { return true }
    return false
}
print("{\"neural_engine\":\(hasANE),\"compute_device_count\":\(devices.count)}")
